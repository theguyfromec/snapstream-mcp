"""SnapStream MCP server (stdio). Run with `python -m snapstream_mcp`.

Safety model
* Reads: ss_call_read and the curated ss_ tools call only methods whose catalog access is "read".
* Writes: only through ss_stage_write (validate, freeze payload, preview, single-use token with a
  10 minute TTL) and ss_commit_write (sends exactly the staged payload once, throttled, audited).
* Blocked methods (credential handling, secret-returning getters, internal plumbing) are never
  callable.
* Credentials, tokens and credential-looking response fields are redacted from all output.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any, Dict, List, Optional, Tuple, Union

from pydantic import BaseModel, Field

try:  # mcp 2.x
    from mcp.server.mcpserver import Context
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # pragma: no cover - mcp 1.x
    from mcp.server.fastmcp import Context  # type: ignore[no-redef]
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef]

from mcp.types import ToolAnnotations

try:
    from mcp.server.elicitation import AcceptedElicitation

    _ELICIT_OK = True
except Exception:  # pragma: no cover - depends on installed SDK
    AcceptedElicitation = ()  # type: ignore[assignment,misc]
    _ELICIT_OK = False

from . import __version__
from .catalog import Catalog, MethodInfo, coerce_params, missing_note
from .client import Redactor, SnapStreamClient, SnapStreamError, apply_paging_defaults
from .config import Config, ConfigError, Settings, load_config
from .formatting import (
    format_response,
    iso_now_plus,
    normalize,
    parse_time_value,
    redact_fields,
    seconds_to_clock,
    unwrap_result,
)
from .staging import WriteStager

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("snapstream_mcp")

INSTRUCTIONS = """\
Tools for one SnapStream instance (TV recording, search, transcripts, program guide).
Start with the curated ss_ tools (ss_search_recordings, ss_list_media_items, ss_get_transcript,
ss_guide_for_channel, ss_list_scheduled_recordings, and so on). For anything else, find the method
with ss_api_catalog, read its parameters with ss_api_describe, and call it with ss_call_read.
Dates in responses are ISO 8601 UTC; pass date-times to tools as ISO 8601 (for example
2026-11-24T20:15:00Z). Unset dates and empty GUIDs come back as null.
Writes are never direct: ss_stage_write returns a preview and token; show the preview to the user,
get explicit approval, then call ss_commit_write(token). Blocked methods cannot be called.
"""

RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
LOCAL = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
COMMIT = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)
COMMIT_META = {"anthropic/requiresUserInteraction": True}

mcp = _Server("snapstream", instructions=INSTRUCTIONS)


# --------------------------------------------------------------------------- runtime
class Runtime:
    """Holds the catalog, config, HTTP client and write stager. Config loads lazily so the server
    starts (and ss_status can explain the problem) even when secrets.yaml is missing or invalid."""

    def __init__(
        self,
        *,
        config: Optional[Config] = None,
        settings: Optional[Settings] = None,
        catalog: Optional[Catalog] = None,
        transport: Any = None,
        client_kwargs: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.settings = settings or Settings.from_env()
        self.catalog = catalog or Catalog.load()
        self._config = config
        self._config_error: Optional[str] = None
        self._transport = transport
        self._client_kwargs = client_kwargs or {}
        self._client: Optional[SnapStreamClient] = None
        self.redact = Redactor()
        if config is not None:
            for v in config.secret_values():
                self.redact.add(v)
        else:
            self._try_load_config()
        self.stager = WriteStager(
            self.catalog, self.settings.audit_file, ttl=self.settings.stage_ttl_seconds, redactor=self.redact
        )

    def _try_load_config(self) -> None:
        try:
            self._config = load_config()
            for v in self._config.secret_values():
                self.redact.add(v)
            self._config_error = None
        except ConfigError as exc:
            self._config_error = str(exc)

    @property
    def config(self) -> Optional[Config]:
        return self._config

    @property
    def config_error(self) -> Optional[str]:
        return self._config_error

    def client(self) -> SnapStreamClient:
        if self._client is None:
            if self._config is None:
                self._try_load_config()
            if self._config is None:
                raise ConfigError(self._config_error or "Configuration is not loaded.")
            self._client = SnapStreamClient(
                self._config, self.settings, transport=self._transport, **self._client_kwargs
            )
            self._client.redact = self.redact  # one shared redactor, so new tokens are masked everywhere
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()


_RUNTIME: Optional[Runtime] = None


def runtime() -> Runtime:
    global _RUNTIME
    if _RUNTIME is None:
        _RUNTIME = Runtime()
    return _RUNTIME


def set_runtime(rt: Optional[Runtime]) -> None:
    """Install a runtime (tests use this to inject a mock transport)."""
    global _RUNTIME
    _RUNTIME = rt


# --------------------------------------------------------------------------- helpers
def _err(message: str, **extra: Any) -> Dict[str, Any]:
    return {"error": runtime().redact(message), **extra}


def _guard(info: Optional[MethodInfo], service: str, method: str) -> Optional[Dict[str, Any]]:
    rt = runtime()
    if info is None:
        return _err(
            f"Unknown method {service}/{method}. Use ss_api_catalog to find the exact name.",
            suggestions=rt.catalog.suggest(service, method),
        )
    if info.access == "blocked":
        return _err(f"{info.key} is blocked and cannot be called through this server: {info.access_reason}", access="blocked")
    if info.access != "read":
        return _err(
            f"{info.key} is a write method. {info.access_reason} ss_call_read only runs read methods. "
            "Use ss_stage_write to prepare it for user approval, then ss_commit_write.",
            access=info.access,
        )
    return None


async def _fetch(service: str, method: str, params: Optional[Dict[str, Any]]) -> Tuple[Optional[MethodInfo], Dict[str, Any], Any, Optional[Dict[str, Any]]]:
    """Validate and send one read call. Returns (info, body_sent, raw_response, error)."""
    rt = runtime()
    info = rt.catalog.get(service, method)
    refusal = _guard(info, service, method)
    if refusal:
        return info, {}, None, refusal
    assert info is not None
    try:
        body, _notes = coerce_params(info, params)
        body = apply_paging_defaults(body, info.param_names)
    except (ValueError, SnapStreamError) as exc:
        return info, {}, None, _err(f"Invalid parameters: {exc}", valid_params=[f"{n}: {t}" for n, t in info.params])
    try:
        client = rt.client()
        raw = await client.call(info.service, info.name, body)
    except ConfigError as exc:
        return info, body, None, _err(str(exc), kind="config")
    except SnapStreamError as exc:
        return info, body, None, {k: (rt.redact(v) if isinstance(v, str) else v) for k, v in exc.to_dict().items()}
    except Exception as exc:  # noqa: BLE001 - never leak a raw traceback
        return info, body, None, _err(f"Unexpected error: {type(exc).__name__}: {exc}")
    return info, body, raw, None


async def _read(
    service: str,
    method: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    drop_empty: bool = True,
    max_chars: Optional[int] = None,
    brief: bool = False,
) -> Dict[str, Any]:
    info, body, raw, error = await _fetch(service, method, params)
    if error:
        return error
    assert info is not None
    rt = runtime()
    limit = max(1000, min(int(max_chars or rt.settings.max_response_chars), 200_000))
    data = raw
    if brief:
        data = _brief_items(normalize(unwrap_result(raw, info.name)))
    out = format_response(data, info.name, max_chars=limit, drop_empty=drop_empty, index=body.get("index"), paged=info.paged)
    result: Dict[str, Any] = {"method": info.key, "params_sent": redact_fields(body)}
    note = missing_note(info, body)
    if note:
        result["note"] = note
    result.update(out)
    return rt.redact.scrub(result)


_BRIEF_KEY = re.compile(
    r"(?i)^(id|itemid|mediaitemid|title|name|episodetitle|seriestitle|showtitle|description|"
    r"channel\w*|\w*channelname|start\w*|end\w*|duration\w*|airdate|airtime|recorded\w*|created\w*|"
    r"\w*callsign\w*|filename|state|status)$"
)


def _brief_items(data: Any) -> Any:
    """Keep the identifying fields of each item (id, title, channel, start, duration). Items with
    none of those fields are returned whole so nothing is silently lost."""
    if isinstance(data, list):
        return [_brief_items(x) for x in data]
    if isinstance(data, dict):
        kept = {k: v for k, v in data.items() if _BRIEF_KEY.match(str(k)) and not isinstance(v, (dict, list))}
        return kept if kept else data
    return data


# ----------------------------------------------------------------- transcript helpers
_TEXT_KEYS = ("text", "caption", "captiontext", "content", "line", "words", "value")
_TIME_KEYS = ("start", "starttime", "startoffset", "begin", "offset", "time", "timestamp", "position", "startseconds", "startms")


def _caption_parts(c: Any) -> Tuple[Optional[float], str, str]:
    """(time value, time kind, text) for one caption of unknown shape."""
    if isinstance(c, str):
        return None, "", c
    if not isinstance(c, dict):
        return None, "", str(c)
    lower = {str(k).lower(): k for k in c}
    text = ""
    for tk in _TEXT_KEYS:
        if tk in lower and isinstance(c[lower[tk]], str):
            text = c[lower[tk]]
            break
    if not text:
        strings = [v for v in c.values() if isinstance(v, str) and not v.startswith("/Date(") and len(v) > 0]
        text = max(strings, key=len) if strings else ""
    t, kind = None, ""
    keys = [lower[k] for k in _TIME_KEYS if k in lower] + [k for k in c if "start" in str(k).lower()]
    for k in keys:
        t, kind = parse_time_value(c[k], str(k))
        if t is not None:
            break
    return t, kind, text


def transcript_window(
    captions: List[Any], *, start_caption: int = 0, offset_seconds: float = 0.0, max_chars: int = 20000, contains: str = ""
) -> Dict[str, Any]:
    parts = [_caption_parts(c) for c in captions]
    epoch0 = next((t for t, k, _ in parts if k == "epoch_s" and t is not None), None)
    rel: List[Optional[float]] = []
    for t, k, _ in parts:
        if t is None:
            rel.append(None)
        elif k == "epoch_s" and epoch0 is not None:
            rel.append(t - epoch0)
        else:
            rel.append(t)
    have_times = any(r is not None for r in rel)
    needle = (contains or "").lower()
    lines: List[str] = []
    used = 0
    i = max(0, int(start_caption))
    next_caption: Optional[int] = None
    matched = 0
    while i < len(parts):
        r = rel[i]
        text = " ".join((parts[i][2] or "").split())
        if (r is not None and r < offset_seconds) or not text or (needle and needle not in text.lower()):
            i += 1
            continue
        line = f"[{seconds_to_clock(r)}] {text}" if r is not None else f"[#{i}] {text}"
        if used + len(line) + 1 > max_chars and lines:
            next_caption = i
            break
        lines.append(line)
        used += len(line) + 1
        matched += 1
        i += 1
    out: Dict[str, Any] = {
        "caption_count": len(captions),
        "returned_lines": matched,
        "time_basis": (
            "seconds from the first caption" if epoch0 is not None else ("offset within the recording" if have_times else "unknown; lines show caption index")
        ),
        "transcript": "\n".join(lines),
        "truncated": next_caption is not None,
    }
    if next_caption is not None:
        out["next_start_caption"] = next_caption
        if rel[next_caption] is not None:
            out["next_offset_seconds"] = round(rel[next_caption], 3)  # type: ignore[arg-type]
        out["hint"] = f"More transcript remains. Call again with start_caption={next_caption}."
    if captions and isinstance(captions[0], dict):
        out["caption_fields"] = sorted(str(k) for k in captions[0].keys())
    return out


# ============================================================================ tools
# ---------------------------------------------------------------- status/catalog
@mcp.tool(annotations=RO)
async def ss_status(check_token: bool = True) -> Dict[str, Any]:
    """Health check for this SnapStream connection. Returns the instance label, API host, whether
    secrets.yaml is valid, whether a Cognito access token can be obtained (check_token=true makes
    one token request; no API call), catalog counts (read/write/blocked), and pending staged
    writes. Never returns credentials or tokens. Call this first if any other tool reports an
    auth or config error."""
    rt = runtime()
    cfg = rt.config
    out: Dict[str, Any] = {"server": "snapstream", "version": __version__}
    if cfg is None and rt.config_error is None:
        rt._try_load_config()
        cfg = rt.config
    if cfg is None:
        out["config"] = {"ok": False, "error": rt.config_error}
    else:
        out["config"] = {"ok": True, "source": cfg.source_path}
        out["instance"] = cfg.instance or None
        out["base_host"] = cfg.base_host
        out["token_host"] = cfg.token_host
        if check_token:
            try:
                client = rt.client()
                await client.get_token()
                out["token"] = {"ok": True, "expires_in_seconds": client.token_seconds_left()}
            except SnapStreamError as exc:
                out["token"] = {"ok": False, **{k: (rt.redact(v) if isinstance(v, str) else v) for k, v in exc.to_dict().items()}}
            except Exception as exc:  # noqa: BLE001
                out["token"] = {"ok": False, "error": rt.redact(f"{type(exc).__name__}: {exc}")}
    counts = rt.catalog.counts()
    out["catalog"] = {k: counts[k] for k in ("services", "methods", "read", "write", "blocked")}
    out["pending_writes"] = rt.stager.pending()["count"]
    out["limits"] = {
        "max_count_per_call": 1000,
        "write_min_interval_seconds": rt.settings.write_min_interval,
        "max_concurrency": rt.settings.max_concurrency,
        "timeout_seconds": rt.settings.timeout_seconds,
        "max_response_chars": rt.settings.max_response_chars,
        "staged_write_ttl_seconds": rt.settings.stage_ttl_seconds,
    }
    out["audit_file"] = rt.settings.audit_file.name
    return out


@mcp.tool(annotations=LOCAL)
def ss_api_catalog(query: str = "", service: str = "", access: str = "", limit: int = 50) -> Dict[str, Any]:
    """Search the offline catalog of all 461 SnapStream API methods (no network call).

    query: space-separated words that must all appear in the service, method name, summary,
      parameter names/types or return type (case-insensitive), e.g. "transcript", "airings channel".
    service: exact service filter: AuthenticationManager, Configuration, FeatureManager,
      FileAccess, LocalConfiguration, Logger, NotifierManager, Search, StreamScheduler, Library,
      Scheduler, TaskManager.
    access: "read" (callable with ss_call_read), "write" (needs ss_stage_write and approval) or
      "blocked" (never callable).
    limit: maximum matches (1..200).
    With no arguments, returns the service list with counts."""
    rt = runtime()
    limit = max(1, min(int(limit), 200))
    if not query and not service and not access:
        counts = rt.catalog.counts()
        return {
            "services": [
                {"service": s["service"], "description": s["description"], **counts["by_service"].get(s["service"], {})}
                for s in rt.catalog.services
            ],
            "totals": {k: counts[k] for k in ("methods", "read", "write", "blocked")},
            "hint": "Pass query, service or access to list methods; then ss_api_describe(service, method).",
        }
    if access and access.lower() not in ("read", "write", "blocked"):
        return _err("access must be read, write or blocked")
    hits = rt.catalog.search(query, service, access, limit=limit + 1)
    return {
        "count": min(len(hits), limit),
        "more_available": len(hits) > limit,
        "matches": [m.brief() for m in hits[:limit]],
    }


@mcp.tool(annotations=LOCAL)
def ss_api_describe(service: str, method: str) -> Dict[str, Any]:
    """Full catalog entry for one method (no network call): summary, access class and the reason
    for it, exact parameter names and types, required permissions and features, return type,
    whether it is paged, an example params object, and which tool to call it with.
    service and method are case-insensitive, e.g. ("Library", "GetTranscript")."""
    rt = runtime()
    info = rt.catalog.get(service, method)
    if info is None:
        return _err(f"Unknown method {service}/{method}.", suggestions=rt.catalog.suggest(service, method))
    return info.full()


@mcp.tool(annotations=RO)
async def ss_call_read(
    service: str,
    method: str,
    params: Optional[Dict[str, Any]] = None,
    compact_output: bool = True,
    max_chars: int = 0,
) -> Dict[str, Any]:
    """Call any READ method from the catalog. Write and blocked methods are refused (stage writes
    with ss_stage_write instead).

    service, method: as listed by ss_api_catalog, e.g. "Scheduler", "GetAiring".
    params: JSON object keyed by the exact parameter names from ss_api_describe. Leave out
      authTicket (removed automatically). Guid values are GUID strings; DateTime values are ISO 8601
      (for example "2026-11-24T20:00:00Z"); ColumnFilter[]/ColumnSort[] may be null. Paged methods
      default to index=0, count=50; count is capped at 1000.
    compact_output: drop null and empty fields (default true).
    max_chars: cap on the JSON size of the result (default from server settings). When capped,
      'truncated' is true and 'hint' says how to page.
    Response dates are ISO 8601 UTC; unset dates and empty GUIDs are null; durations gain a
    '<field>_seconds' number."""
    return await _read(service, method, params, drop_empty=compact_output, max_chars=max_chars or None)


# ------------------------------------------------------------------- write gate
@mcp.tool(annotations=LOCAL)
def ss_stage_write(service: str, method: str, params: Optional[Dict[str, Any]] = None, reason: str = "") -> Dict[str, Any]:
    """Stage a WRITE (does not execute anything). Only methods with access "write" can be staged;
    read methods are refused (use ss_call_read) and blocked methods are always refused.

    params are validated against the catalog parameter names and frozen exactly as they will be
    sent (DateTime values converted to the API's /Date(ms)/ form, authTicket removed).
    reason: one line on why, recorded in the audit log.
    Returns a preview and a single-use token valid for 10 minutes. Show the preview to the user
    and get explicit approval before calling ss_commit_write(token)."""
    return runtime().stager.stage(service, method, params, reason)


class _ConfirmWrite(BaseModel):
    confirm: bool = Field(description="True to send this write to SnapStream now, false to abort.")


def _client_supports_elicitation(ctx: Any) -> bool:
    """True when the connected client declared the elicitation capability. False when there is
    no context or no client request (a direct in-process call) or the client declared no
    elicitation; only then may a commit proceed on the token alone."""
    if ctx is None:
        return False
    try:
        caps = getattr(ctx, "client_capabilities", None)  # mcp 2.x
    except Exception:  # noqa: BLE001 - outside a request there is no client to ask
        return False
    if caps is None:
        try:  # mcp 1.x keeps them on the initialize params
            params = ctx.session.client_params
            caps = getattr(params, "capabilities", None) if params is not None else None
        except Exception:  # noqa: BLE001
            return False
    return getattr(caps, "elicitation", None) is not None


@mcp.tool(annotations=COMMIT, meta=COMMIT_META)
async def ss_commit_write(token: str, ctx: Context) -> Dict[str, Any]:
    """Execute a staged write. THIS IS THE ONLY TOOL THAT CHANGES SNAPSTREAM.
    Sends exactly the payload frozen by ss_stage_write, once, through the write throttle, and
    records it in the audit log. The token is consumed by any attempt; re-stage to retry.
    Only call after the user has approved the preview. Where the client supports elicitation,
    the user is asked to confirm again inside this call."""
    rt = runtime()
    sw, err = rt.stager.take(token)
    if err:
        return err
    assert sw is not None
    mode = "unsupported"
    if _ELICIT_OK and _client_supports_elicitation(ctx):
        # The client declared elicitation, so it must confirm. Fail closed: a decline, a cancel,
        # an accept without confirm == True, or any error (timeout, transport failure, a reply
        # that does not match the schema) sends nothing.
        confirmed = False
        try:
            res = await ctx.elicit(message=f"Confirm SnapStream write:\n\n{sw.preview}\n\nSend it now?", schema=_ConfirmWrite)
            if isinstance(res, AcceptedElicitation):
                confirmed = getattr(res.data, "confirm", False) is True
                mode = "accepted" if confirmed else "declined"
            else:
                mode = "canceled" if getattr(res, "action", "") == "cancel" else "declined"
        except Exception as exc:  # noqa: BLE001 - any elicitation failure counts as not confirmed
            mode = f"error:{type(exc).__name__}"
        if not confirmed:
            rt.stager.audit_write("commit_declined", sw, elicit_mode=mode)
            reason = (
                "The user declined." if mode in ("declined", "canceled")
                else "The confirmation prompt failed, so nothing was sent."
            )
            return {"status": "aborted", "method": sw.key, "elicit_mode": mode, "reason": f"{reason} The token is spent; stage again to retry."}
    try:
        client = rt.client()
    except ConfigError as exc:
        rt.stager.audit_write("commit_refused_config", sw, elicit_mode=mode)
        return _err(str(exc), kind="config")
    result = await rt.stager.execute(sw, client, elicit_mode=mode)
    if result.get("status") == "committed":
        raw = result.pop("raw", None)
        result["confirmation"] = mode
        result.update(format_response(raw, sw.method, max_chars=rt.settings.max_response_chars))
    return rt.redact.scrub(result)


@mcp.tool(annotations=LOCAL)
def ss_cancel_staged(token: str) -> Dict[str, Any]:
    """Discard a staged write without executing it. Tokens also expire on their own after 10 minutes."""
    return runtime().stager.cancel(token)


@mcp.tool(annotations=LOCAL)
def ss_pending_writes() -> Dict[str, Any]:
    """List staged writes that have not been committed: token, method, reason, payload hash and
    seconds until expiry."""
    return runtime().stager.pending()


# ------------------------------------------------------------- curated: Search
# The search "type" parameters are .NET flag enums serialized as integers; the string names in the
# reference (e.g. "Default") are rejected with HTTP 400. Values below were observed live on the
# demo instance (2026-11-24): for recordings, 2 matched program info (24 recording-level hits for
# "congress") and 4 matched closed captions (about 121,000 caption-level hits, each with an offset
# into its recording); 1 matched nothing. For the guide, 1 and 2 each match different airing text
# fields; 255 sets every bit.
RECORDING_SEARCH_TYPES = {"captions": 4, "program_info": 2, "all": 255}
GUIDE_SEARCH_TYPES = {"all": 255}


def _search_type(value: Union[str, int], names: Dict[str, int]) -> int:
    if isinstance(value, bool):
        raise ValueError("type must be a name or an integer flag value")
    if isinstance(value, int):
        return value
    key = str(value).strip().lower()
    if key in names:
        return names[key]
    if key.lstrip("-").isdigit():
        return int(key)
    raise ValueError(f"unknown search type {value!r}; use one of {sorted(names)} or an integer flag value")


@mcp.tool(annotations=RO)
async def ss_search_recordings(
    search: str,
    index: int = 0,
    count: int = 25,
    type: Union[str, int] = "captions",
    highlightStart: str = "<em>",
    highlightEnd: str = "</em>",
) -> Dict[str, Any]:
    """Full-text search across recorded TV. Calls Search/SearchRecordings.
    search: the words or phrase to find, e.g. "election night".
    index, count: paging (count 1..1000; 25 is a good default for agents).
    type: what to search. "captions" (default) searches closed captions and returns caption-level
      hits, each with its recording and an offset (End) into it, so many hits can share one
      recording. "program_info" searches guide metadata and returns one hit per recording. "all"
      sets every flag. An integer flag value is passed through as-is.
    highlightStart/highlightEnd: markers wrapped around matched words in snippets.
    Returns NumberOfResults and Results; use a result's Id with ss_get_media_item, ss_get_transcript
    or ss_search_in_recording."""
    try:
        t = _search_type(type, RECORDING_SEARCH_TYPES)
    except ValueError as exc:
        return {"error": str(exc), "kind": "invalid_argument"}
    return await _read("Search", "SearchRecordings", {
        "search": search, "index": index, "count": count, "type": t,
        "highlightStart": highlightStart, "highlightEnd": highlightEnd,
    })


@mcp.tool(annotations=RO)
async def ss_count_recording_matches(search: str, type: Union[str, int] = "captions") -> Dict[str, Any]:
    """How many hits a recording search has, before paging. Calls Search/SearchRecordingsCount and
    returns NumberOfResults. type takes the same values as ss_search_recordings ("captions" counts
    caption-level hits, "program_info" counts recordings)."""
    try:
        t = _search_type(type, RECORDING_SEARCH_TYPES)
    except ValueError as exc:
        return {"error": str(exc), "kind": "invalid_argument"}
    return await _read("Search", "SearchRecordingsCount", {"search": search, "type": t})


@mcp.tool(annotations=RO)
async def ss_search_in_recording(
    search: str,
    mediaItem: str,
    index: int = 0,
    count: int = 50,
    highlightStart: str = "<em>",
    highlightEnd: str = "</em>",
) -> Dict[str, Any]:
    """Find every mention of a term inside ONE recording's transcript, with timecodes (block-level
    accuracy). Calls Search/SearchRecordingWithRefinement.
    mediaItem: the media item GUID (from ss_search_recordings or ss_list_media_items).
    Useful for picking clip in and out points."""
    return await _read("Search", "SearchRecordingWithRefinement", {
        "search": search, "mediaItem": mediaItem, "index": index, "count": count,
        "highlightStart": highlightStart, "highlightEnd": highlightEnd,
    })


@mcp.tool(annotations=RO)
async def ss_search_guide(
    search: str,
    index: int = 0,
    count: int = 25,
    type: Union[str, int] = "all",
    highlightStart: str = "<em>",
    highlightEnd: str = "</em>",
) -> Dict[str, Any]:
    """Search the program guide for upcoming or past airings of a show or topic. Calls
    Search/SearchAirings. type: "all" (default) searches every guide text field; an integer flag
    value (1 or 2 select single fields) is passed through as-is. Use the airing id with
    ss_get_airing."""
    try:
        t = _search_type(type, GUIDE_SEARCH_TYPES)
    except ValueError as exc:
        return {"error": str(exc), "kind": "invalid_argument"}
    return await _read("Search", "SearchAirings", {
        "search": search, "index": index, "count": count, "type": t,
        "highlightStart": highlightStart, "highlightEnd": highlightEnd,
    })


# ------------------------------------------------------------ curated: Library
@mcp.tool(annotations=RO)
async def ss_list_media_items(index: int = 0, count: int = 50, brief: bool = True) -> Dict[str, Any]:
    """Page through the recordings and clips in the Library. Calls Library/GetMediaItems.
    index, count: paging (count 1..1000).
    brief: true keeps identifying fields per item (id, title, channel, start, end, duration);
      false returns full items. Use ss_get_media_item for one item's full detail."""
    return await _read("Library", "GetMediaItems", {"index": index, "count": count}, brief=brief)


@mcp.tool(annotations=RO)
async def ss_get_media_item(itemId: str) -> Dict[str, Any]:
    """Full detail for one recording or clip (a Media Item). Calls Library/GetMediaItem.
    itemId: media item GUID."""
    return await _read("Library", "GetMediaItem", {"itemId": itemId})


@mcp.tool(annotations=RO)
async def ss_get_transcript(
    itemId: str,
    start_caption: int = 0,
    offset_seconds: float = 0.0,
    max_chars: int = 20000,
    contains: str = "",
) -> Dict[str, Any]:
    """Read the transcript (captions) of one media item as timestamped lines. Calls
    Library/GetTranscript. Long transcripts are windowed:
    itemId: media item GUID.
    start_caption: caption index to start from (use next_start_caption from a previous call).
    offset_seconds: skip captions before this many seconds into the recording.
    max_chars: size of the returned text window (500..100000).
    contains: optional case-insensitive filter; only captions containing this text are returned.
    For finding a term with exact timecodes, ss_search_in_recording is more precise."""
    info, body, raw, error = await _fetch("Library", "GetTranscript", {"itemId": itemId})
    if error:
        return error
    captions = normalize(unwrap_result(raw, "GetTranscript"))
    if captions is None:
        return {"method": "Library/GetTranscript", "itemId": itemId, "caption_count": 0, "transcript": "", "note": "No transcript returned for this item."}
    rt = runtime()
    if not isinstance(captions, list):
        return rt.redact.scrub(
            {"method": "Library/GetTranscript", **format_response(raw, "GetTranscript", max_chars=rt.settings.max_response_chars)}
        )
    window = transcript_window(
        redact_fields(captions),
        start_caption=start_caption,
        offset_seconds=max(0.0, float(offset_seconds)),
        max_chars=max(500, min(int(max_chars), 100_000)),
        contains=contains,
    )
    return rt.redact.scrub({"method": "Library/GetTranscript", "itemId": itemId, **window})


@mcp.tool(annotations=RO)
async def ss_get_smart_chapters(itemId: str) -> Dict[str, Any]:
    """Smart chapters (an outline of topics with times) for one media item. Read this before
    pulling transcript windows of a long recording. Calls Library/GetSmartChapters."""
    return await _read("Library", "GetSmartChapters", {"itemId": itemId})


@mcp.tool(annotations=RO)
async def ss_list_clips_and_bookmarks(parentId: str, index: int = 0, count: int = 100) -> Dict[str, Any]:
    """Clips and bookmarks already made from a recording. Calls Library/GetClipsAndBookmarks.
    parentId: the source media item GUID. index, count: paging (count 1..1000)."""
    return await _read("Library", "GetClipsAndBookmarks", {"parentId": parentId, "index": index, "count": count})


@mcp.tool(annotations=RO)
async def ss_get_bookmark(itemId: str) -> Dict[str, Any]:
    """Detail for one bookmark or clip segment. Calls Library/GetBookmarkById.
    itemId: the bookmark GUID (from ss_list_clips_and_bookmarks)."""
    return await _read("Library", "GetBookmarkById", {"itemId": itemId})


@mcp.tool(annotations=RO)
async def ss_list_library_channels() -> Dict[str, Any]:
    """Every channel in current lineups or past recordings, with channel ids. Calls
    Library/GetAllPastAndCurrentChannels. Use ss_list_channels for the paged current-lineup list."""
    return await _read("Library", "GetAllPastAndCurrentChannels", {})


@mcp.tool(annotations=RO)
async def ss_storage_status() -> Dict[str, Any]:
    """Recording storage drives and their details (capacity and usage where the API reports it).
    Calls Library/GetDrives. For one drive's usage numbers use
    ss_call_read("Library", "GetDriveStats", {"driveId": ...})."""
    return await _read("Library", "GetDrives", {})


# ---------------------------------------------------------- curated: Scheduler
@mcp.tool(annotations=RO)
async def ss_list_channels(index: int = 0, count: int = 200, displayedOnly: bool = True) -> Dict[str, Any]:
    """Paged list of channels in the current lineups, used to turn a channel name or number into the
    channelId other tools need. Calls Scheduler/GetChannels.
    displayedOnly: true limits to channels shown in the guide."""
    return await _read("Scheduler", "GetChannels", {"index": index, "count": count, "displayedOnly": displayedOnly})


@mcp.tool(annotations=RO)
async def ss_whats_on(channelIds: List[str], time: str = "now") -> Dict[str, Any]:
    """What is airing on these channels at one moment. Calls Scheduler/GetAiringsAtTime.
    channelIds: list of channel GUIDs (at most 50).
    time: ISO 8601 date-time (e.g. "2026-11-24T20:15:00Z") or "now"."""
    if not isinstance(channelIds, list) or not channelIds:
        return _err("channelIds must be a non-empty list of channel GUIDs.")
    if len(channelIds) > 50:
        return _err("At most 50 channelIds per call.")
    return await _read("Scheduler", "GetAiringsAtTime", {"channelIds": channelIds, "time": time})


@mcp.tool(annotations=RO)
async def ss_guide_for_channel(
    channelId: str,
    rangeBegin: str = "",
    rangeEnd: str = "",
    index: int = 0,
    count: int = 200,
) -> Dict[str, Any]:
    """Program guide for one channel: airings overlapping a time range, including the show already
    in progress at rangeBegin. Calls Scheduler/GetAiringsByChannelAndTimeRange.
    rangeBegin, rangeEnd: ISO 8601 date-times; default now to now+6h."""
    return await _read("Scheduler", "GetAiringsByChannelAndTimeRange", {
        "index": index, "count": count, "channelId": channelId,
        "rangeBegin": rangeBegin or iso_now_plus(0), "rangeEnd": rangeEnd or iso_now_plus(6),
    })


@mcp.tool(annotations=RO)
async def ss_guide_by_time(startBegin: str = "", startEnd: str = "", index: int = 0, count: int = 200) -> Dict[str, Any]:
    """Cross-channel guide slice: airings that START within a window. Calls
    Scheduler/GetAiringsByStartTimeRange. startBegin, startEnd: ISO 8601; default now to now+2h."""
    return await _read("Scheduler", "GetAiringsByStartTimeRange", {
        "index": index, "count": count,
        "startBegin": startBegin or iso_now_plus(0), "startEnd": startEnd or iso_now_plus(2),
    })


@mcp.tool(annotations=RO)
async def ss_get_airing(id: str) -> Dict[str, Any]:
    """Full detail for one guide airing (title, episode, channel, times, series or movie ids).
    Calls Scheduler/GetAiring. id: airing GUID."""
    return await _read("Scheduler", "GetAiring", {"id": id})


@mcp.tool(annotations=RO)
async def ss_list_scheduled_recordings(
    index: int = 0,
    count: int = 100,
    filters: Optional[List[Dict[str, Any]]] = None,
    sorts: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """In-progress plus upcoming scheduled recordings. Calls Scheduler/GetCurrentAndUpcomingRecordings.
    filters/sorts: optional ColumnFilter[] {column, operator, value} and ColumnSort[] {column, order};
    the exact column names are not documented, so leave them null unless you have confirmed them."""
    return await _read("Scheduler", "GetCurrentAndUpcomingRecordings", {"index": index, "count": count, "filters": filters, "sorts": sorts})


@mcp.tool(annotations=RO)
async def ss_list_current_recordings(
    includeLiveTv: bool = False, includeStarting: bool = True, index: int = 0, count: int = 100
) -> Dict[str, Any]:
    """What is recording right now. Calls Scheduler/GetCurrentRecordings.
    includeLiveTv: include Live TV session recordings. includeStarting: include ones just starting."""
    return await _read("Scheduler", "GetCurrentRecordings", {
        "includeLiveTv": includeLiveTv, "includeStarting": includeStarting, "index": index, "count": count,
    })


@mcp.tool(annotations=RO)
async def ss_list_recent_recordings(
    includePartialRecordings: bool = True,
    index: int = 0,
    count: int = 50,
    filters: Optional[List[Dict[str, Any]]] = None,
    sorts: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Recently completed recordings, including partial or failed ones when
    includePartialRecordings is true. Calls Scheduler/GetRecentRecordings. Good for "did last
    night's recording work". filters/sorts as in ss_list_scheduled_recordings."""
    return await _read("Scheduler", "GetRecentRecordings", {
        "includePartialRecordings": includePartialRecordings, "index": index, "count": count,
        "filters": filters, "sorts": sorts,
    })


@mcp.tool(annotations=RO)
async def ss_list_recording_jobs(
    index: int = 0,
    count: int = 100,
    filters: Optional[List[Dict[str, Any]]] = None,
    sorts: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Standing recording rules (series, manual, 24/7 jobs) behind the scheduled recordings.
    Calls Scheduler/GetRecordingJobs. Detail for one job: ss_call_read("Scheduler", "GetRecordingJob", {"id": ...})."""
    return await _read("Scheduler", "GetRecordingJobs", {"index": index, "count": count, "filters": filters, "sorts": sorts})


@mcp.tool(annotations=RO)
async def ss_scheduler_status() -> Dict[str, Any]:
    """Scheduler health in one call: Scheduler/GetStats, Scheduler/GetUpdateStatus (guide update)
    and Scheduler/GetLastRebuild (last schedule build)."""
    out: Dict[str, Any] = {}
    for m in ("GetStats", "GetUpdateStatus", "GetLastRebuild"):
        r = await _read("Scheduler", m, {})
        out[m] = r.get("result", r.get("result_text", r))
    return out


# --------------------------------------------------------- curated: TaskManager
@mcp.tool(annotations=RO)
async def ss_get_job(listId: str) -> Dict[str, Any]:
    """Status of a task list (clip render, screenshot, export, transcription job). Calls
    TaskManager/GetTaskList. listId: the task list GUID returned by a committed write such as
    TaskManager/ClipFile."""
    return await _read("TaskManager", "GetTaskList", {"listId": listId})


# ------------------------------------------------------- curated: identity
@mcp.tool(annotations=RO)
async def ss_whoami() -> Dict[str, Any]:
    """Email address of the SnapStream user this API client acts as. Calls
    AuthenticationManager/GetUserEmail. A quick end-to-end connectivity check."""
    return await _read("AuthenticationManager", "GetUserEmail", {})


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    runtime()  # load catalog and config up front so problems show in ss_status
    mcp.run()


if __name__ == "__main__":
    main()
