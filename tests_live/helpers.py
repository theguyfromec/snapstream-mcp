"""PYTEST_DONT_REWRITE

Shared helpers for the live suite: the session object (one event loop, the real Runtime, a write
firewall), discovery of real ids, output checks (shape, leaks) and the report.

Nothing here prints response values, tokens or credentials. Assertion messages carry method
names, key paths, error kinds, HTTP statuses and the server's own redacted error line only.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Tuple

from snapstream_mcp import server
from snapstream_mcp.catalog import Catalog, MethodInfo
from snapstream_mcp.client import SnapStreamError
from snapstream_mcp.formatting import DURATION_RE, is_guid, iso_duration_seconds

EMPTY_GUID = "00000000-0000-0000-0000-000000000000"
TITLE_PREFIX = "MCP live test "
MESSAGE_LIMIT = 160


# ------------------------------------------------------------------- small utils
def utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def iso(dt: _dt.datetime) -> str:
    return dt.astimezone(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def usable_guid(value: Any) -> bool:
    return is_guid(value) and value != EMPTY_GUID


def short(text: Any, limit: int = MESSAGE_LIMIT) -> str:
    s = " ".join(str(text or "").split())
    return s[:limit] + ("..." if len(s) > limit else "")


def describe_error(out: Any) -> str:
    """kind, HTTP status and the server's first (already redacted) error line. Never a body."""
    if not isinstance(out, dict):
        return f"unexpected return type {type(out).__name__}"
    parts = []
    if out.get("kind"):
        parts.append(f"kind={out['kind']}")
    if out.get("http_status") is not None:
        parts.append(f"http_status={out['http_status']}")
    if out.get("access"):
        parts.append(f"access={out['access']}")
    if out.get("status"):
        parts.append(f"status={out['status']}")
    if out.get("error"):
        parts.append(f"error={short(out['error'])}")
    return ", ".join(parts) or "no error details"


def result_of(out: Dict[str, Any]) -> Any:
    """The 'result' value of a curated tool response (None when absent or capped to text)."""
    return out.get("result") if isinstance(out, dict) else None


def as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def first_id(items: Iterable[Any], keys: Tuple[str, ...] = ("Id",)) -> Optional[str]:
    for it in items or []:
        if isinstance(it, dict):
            for k in keys:
                if usable_guid(it.get(k)):
                    return it[k]
    return None


def titles_of(d: Dict[str, Any]) -> List[str]:
    return [d[k] for k in ("ClipTitle", "Title", "NonHighlighted_ClipTitle") if isinstance(d.get(k), str)]


# ------------------------------------------------------------------- output checks
ISO_DT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
SMART_CHAPTER_KEYS = frozenset(("Start", "End"))


def shape_issues(obj: Any, tick_keys: Iterable[str], path: str = "$", limit: int = 20) -> List[str]:
    """Problems with a normalized response, by key path (never values): an unconverted /Date(),
    an unparsable ISO date, a tick field or ISO duration without its _seconds sibling."""
    ticks = frozenset(tick_keys)
    issues: List[str] = []

    def walk(o: Any, p: str) -> None:
        if len(issues) >= limit:
            return
        if isinstance(o, dict):
            for k, v in o.items():
                kp = f"{p}.{k}"
                if isinstance(v, str):
                    if "/Date(" in v:
                        issues.append(f"{kp}: WCF /Date() value not converted")
                    elif ISO_DT_RE.match(v):
                        try:
                            _dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
                        except ValueError:
                            issues.append(f"{kp}: unparsable ISO date-time")
                    elif DURATION_RE.match(v) and iso_duration_seconds(v) is not None and f"{k}_seconds" not in o:
                        issues.append(f"{kp}: ISO duration without {k}_seconds")
                elif k in ticks and isinstance(v, int) and not isinstance(v, bool) and f"{k}_seconds" not in o:
                    issues.append(f"{kp}: tick field without {k}_seconds")
                walk(v, kp)
        elif isinstance(o, list):
            for i, v in enumerate(o[:200]):
                walk(v, f"{p}[{i}]")

    walk(obj, path)
    return issues


_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")
_BEARER = re.compile(r"(?i)\bbearer\s+(?!\[REDACTED\])[A-Za-z0-9._~+/=-]{16,}")
_KV = re.compile(
    r'(?i)"?(?:access_token|id_token|refresh_token|client_secret|api_key|x-api-key)"?\s*[:=]\s*"?(?!\[REDACTED\])[^"\s,&}\]]{4,}'
)
_URL_CRED = re.compile(
    r"(?i)[?&](?:token|access_token|id_token|sig|signature|x-amz-signature|x-amz-credential|x-amz-security-token)="
    r"(?!\[REDACTED\])[^&#\s\"'<>]+"
)


def leak_labels(text: str, known_values: Iterable[str]) -> List[str]:
    """Labels (never the values) of secret-looking content found in text."""
    found: List[str] = []
    for i, v in enumerate(known_values):
        if v and v in text:
            found.append(f"configured secret or live access token #{i + 1}")
    for label, pat in (("JWT-shaped string", _JWT), ("bearer token", _BEARER),
                       ("credential key/value pair", _KV), ("credential URL query parameter", _URL_CRED)):
        if pat.search(text):
            found.append(label)
    return found


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False)


# ------------------------------------------------------------- failure output
SAFE_TB_STYLES = ("short", "line", "no", "native")


def harden_failure_options(option: Any) -> None:
    """Make pytest's failure output leave out frame values. The default --tb=auto (and --tb=long
    or --full-trace) prints every frame's arguments, and -l/--showlocals prints its locals, which
    here are fixture items and response dicts. Forced on the parsed options, so an extra --tb=long
    or -l passed through live_test.py cannot turn them back on."""
    if getattr(option, "tbstyle", "auto") not in SAFE_TB_STYLES:
        option.tbstyle = "short"
    option.showlocals = False
    option.fulltrace = False


def strip_frame_values(longrepr: Any) -> int:
    """Drop the function-argument and locals sections from every traceback entry of a failure
    repr, whatever options produced it. Returns how many sections were removed."""
    removed = 0
    chain = getattr(longrepr, "chain", None)
    tracebacks = [c[0] for c in chain] if chain else [getattr(longrepr, "reprtraceback", None)]
    for tb in tracebacks:
        for entry in getattr(tb, "reprentries", None) or []:
            for attr in ("reprfuncargs", "reprlocals"):
                if getattr(entry, attr, None) is not None:
                    setattr(entry, attr, None)
                    removed += 1
    return removed


# ------------------------------------------------------------------ write firewall
class WriteBlocked(SnapStreamError):
    pass


class WriteFirewall:
    """Sits in front of SnapStreamClient.call. Reads pass. A write passes only when the run was
    started with --live-writes (writes_allowed, fixed at construction) AND the write tier has
    turned the firewall on (enabled), and only: ClipFile/AiClipDesigner up to max_creates per run,
    and DeleteMediaItem for an id the cleanup verified and registered (one delete per create).
    Everything else is refused before it leaves this machine and recorded as a violation."""

    CREATE_METHODS = frozenset(("TaskManager/ClipFile", "TaskManager/AiClipDesigner"))
    DELETE_METHOD = "Library/DeleteMediaItem"

    def __init__(self, max_creates: int = 2, writes_allowed: bool = False) -> None:
        self.max_creates = max_creates
        self.writes_allowed = bool(writes_allowed)
        self.enabled = False
        self.creates = 0
        self.deletes = 0
        self._deletable: set = set()
        self.violations: List[str] = []

    def allow_delete(self, item_id: str) -> None:
        self._deletable.add(str(item_id).lower())

    def authorize(self, key: str, params: Optional[Dict[str, Any]]) -> None:
        def refuse(why: str) -> None:
            self.violations.append(f"{key}: {why}")
            raise WriteBlocked(
                f"{key} refused by the live test write firewall: {why}", kind="test_firewall", sent=False
            )

        if not self.writes_allowed:
            refuse("the run was not started with --live-writes")
        if not self.enabled:
            refuse("writes are not enabled in this tier")
        if key in self.CREATE_METHODS:
            if self.creates >= self.max_creates:
                refuse(f"hard cap of {self.max_creates} commits per run reached")
            self.creates += 1
            return
        if key == self.DELETE_METHOD:
            item = str((params or {}).get("itemId", "")).lower()
            if item not in self._deletable:
                refuse("item id was not created and verified by this run")
            if self.deletes >= self.creates:
                refuse("more deletes than creates")
            self._deletable.discard(item)
            self.deletes += 1
            return
        refuse("method is not on the live test allowlist")


def install_firewall(client: Any, catalog: Catalog, fw: WriteFirewall, call_log: List[Tuple[str, bool]]) -> None:
    """Wrap client.call (an instance attribute, so the server's own code goes through it)."""
    original = client.call

    async def guarded(service: str, method: str, params: Optional[Dict[str, Any]] = None, *, write: bool = False, meta=None):
        info = catalog.get(service, method)
        key = info.key if info else f"{service}/{method}"
        call_log.append((key, bool(write)))
        if write or info is None or info.access != "read":
            fw.authorize(key, params)
        return await original(service, method, params, write=write, meta=meta)

    client.call = guarded


# ---------------------------------------------------------------------- session
class LiveSession:
    def __init__(self, rt: "server.Runtime", loop: asyncio.AbstractEventLoop, firewall: WriteFirewall,
                 report: "LiveReport", run_stamp: str, expected_host: str, options: Dict[str, Any]) -> None:
        self.rt = rt
        self.loop = loop
        self.firewall = firewall
        self.report = report
        self.run_stamp = run_stamp
        self.expected_host = expected_host
        self.options = options
        self.call_log: List[Tuple[str, bool]] = []
        self.discovery = Discovery(self)

    # --- plumbing
    def run(self, coro: Awaitable[Any], timeout: Optional[float] = None) -> Any:
        if timeout is not None:
            coro = asyncio.wait_for(coro, timeout)
        return self.loop.run_until_complete(coro)

    @property
    def client(self) -> Any:
        return self.rt.client()

    def calls_since(self, mark: int) -> List[Tuple[str, bool]]:
        return self.call_log[mark:]

    def usage(self) -> Dict[str, Any]:
        return self.rt.usage()

    def secret_values(self) -> List[str]:
        # The server's own redactor holds the configured secrets and the live access token.
        return list(getattr(self.rt.redact, "_values", []))

    def scrub(self, text: str) -> str:
        return self.rt.redact(text)

    def leaks(self, obj: Any) -> List[str]:
        return leak_labels(obj if isinstance(obj, str) else dumps(obj), self.secret_values())

    # --- assertions
    def assert_no_leaks(self, obj: Any, label: str) -> None:
        found = self.leaks(obj)
        assert not found, f"{label}: secret-looking value in output: {', '.join(found)}"

    def assert_ok(self, out: Any, label: str) -> Dict[str, Any]:
        assert isinstance(out, dict), f"{label}: returned {type(out).__name__}, not a dict"
        assert "error" not in out, f"{label}: {describe_error(out)}"
        self.assert_no_leaks(out, label)
        return out

    def assert_shape(self, out: Dict[str, Any], label: str, method: str = "") -> None:
        ticks = set(self.rt.catalog.tick_fields) | (SMART_CHAPTER_KEYS if method == "GetSmartChapters" else set())
        issues = shape_issues(result_of(out), ticks)
        assert not issues, f"{label}: shape issues: {'; '.join(issues[:8])}"


# --------------------------------------------------------------------- discovery
class Discovery:
    """Finds real ids on the instance once per run (cached). Every finder returns None plus a
    reason when nothing suitable exists. A finder that got a tool error (an 'error' key, or an
    exception) and found nothing also records it in errors: the conftest fixture then fails the
    dependent tests instead of skipping them, so a broken tool cannot hide behind a skip."""

    CAPTION_WORDS = ("news", "president", "weather", "today", "people")
    SEARCH_PAGE = 50          # caption hits per search page
    SEARCH_PAGES = 2          # pages per word
    LIBRARY_PAGE = 50
    LIBRARY_PAGES = 2
    MAX_CHECKS = 15           # GetMediaItem checks per source (caption search, then Library)
    MIN_SOURCE_SECONDS = 120.0

    def __init__(self, live: LiveSession) -> None:
        self.live = live
        self._cache: Dict[str, Any] = {}
        self.reasons: Dict[str, str] = {}
        self.errors: Dict[str, str] = {}

    async def _aget(self, name: str, finder: Callable[[], Awaitable[Any]]) -> Any:
        if name not in self._cache:
            try:
                self._cache[name] = await finder()
            except Exception as exc:  # noqa: BLE001 - a discovery crash fails the dependent tests
                self._cache[name] = None
                self.errors[name] = self.reasons[name] = f"discovery raised {type(exc).__name__}"
        return self._cache[name]

    def _get(self, name: str, finder: Callable[[], Awaitable[Any]]) -> Any:
        """Sync entry point for tests and fixtures (finders await _aget instead)."""
        if name in self._cache:
            return self._cache[name]
        return self.live.run(self._aget(name, finder))

    def found(self) -> Dict[str, bool]:
        return {k: v is not None for k, v in self._cache.items()}

    def tool_error(self, name: str) -> Optional[str]:
        """The tool error that left this fixture unfound, or None (found, or genuinely empty)."""
        return self.errors.get(name)

    @staticmethod
    def _err(label: str, out: Any) -> Optional[str]:
        """'label: kind, status, error' when out is a tool error (or not a dict), else None."""
        if not isinstance(out, dict) or "error" in out:
            return f"{label}: {describe_error(out)}"
        return None

    def _miss(self, name: str, reason: str, errs: List[str]) -> None:
        """Record why a fixture was not found; a tool error, when there was one, comes first."""
        if errs:
            self.errors[name] = "; ".join(errs)
            self.reasons[name] = f"tool error: {self.errors[name]} ({reason})"
        else:
            self.reasons[name] = reason

    # --- media item with captions
    def media_item(self) -> Optional[Dict[str, Any]]:
        return self._get("media_item", self._find_media_item)

    async def _check_media_item(self, iid: str, rejected: Dict[str, int], errs: List[str]) -> Optional[Dict[str, Any]]:
        """The item when it is a completed, non-clip recording of MIN_SOURCE_SECONDS with captions."""
        mi = await server.ss_get_media_item(iid)
        item = result_of(mi)
        if self._err("ss_get_media_item", mi) or not isinstance(item, dict):
            rejected["get_failed"] += 1  # an indexed item can be gone; counted, not a tool error
            return None
        if item.get("InProgress") or item.get("Clip") or item.get("Deleted") or not all(
            usable_guid(item.get(k)) for k in ("FolderPathId", "FolderId")
        ) or not item.get("Filename"):
            rejected["unsuitable"] += 1
            return None
        dur = item.get("Duration_seconds")
        if not isinstance(dur, (int, float)) or dur < self.MIN_SOURCE_SECONDS:
            rejected["too_short"] += 1
            return None
        tr = await server.ss_get_transcript(iid, max_chars=500)
        err = self._err("ss_get_transcript", tr)
        if err:
            rejected["transcript_error"] += 1
            if not any(e.startswith("ss_get_transcript") for e in errs):
                errs.append(err)
            return None
        if not tr.get("caption_count"):
            rejected["no_captions"] += 1
            return None
        return item

    async def _caption_page(self, query: str, index: int) -> Tuple[List[Any], Optional[str]]:
        out = await server.ss_search_recordings(query, index=index, count=self.SEARCH_PAGE, type="captions")
        err = self._err("ss_search_recordings", out)
        if err:
            return [], err
        res = result_of(out)
        return as_list(res.get("Results") if isinstance(res, dict) else None), None

    async def _find_media_item(self) -> Optional[Dict[str, Any]]:
        rejected = {"hits_in_progress_or_clip": 0, "get_failed": 0, "unsuitable": 0, "too_short": 0,
                    "no_captions": 0, "transcript_error": 0}
        errs: List[str] = []
        seen: set = set()
        checked = {"search": 0, "library": 0}

        # 1. Caption searches (newest first). Hits on clips and on items still recording are
        # dropped before they use up a check; the query also excludes clips. A query the index
        # cannot parse returns zero hits with no error, so a word with no hits is retried plain.
        for word in self.CAPTION_WORDS:
            query = f"{word} -IS:CLIP"
            for page in range(self.SEARCH_PAGES):
                hits, err = await self._caption_page(query, page * self.SEARCH_PAGE)
                if err is None and not hits and page == 0:
                    query = word
                    hits, err = await self._caption_page(query, 0)
                if err:
                    if err not in errs:
                        errs.append(err)
                    break
                for r in hits:
                    if not isinstance(r, dict):
                        continue
                    if r.get("InProgress") or r.get("Clip"):
                        rejected["hits_in_progress_or_clip"] += 1
                        continue
                    iid = r.get("ItemId") if usable_guid(r.get("ItemId")) else r.get("Id")
                    if not usable_guid(iid) or iid.lower() in seen:
                        continue
                    if checked["search"] >= self.MAX_CHECKS:
                        break
                    seen.add(iid.lower())
                    checked["search"] += 1
                    item = await self._check_media_item(iid, rejected, errs)
                    if item is not None:
                        return item
                if len(hits) < self.SEARCH_PAGE or checked["search"] >= self.MAX_CHECKS:
                    break
            if checked["search"] >= self.MAX_CHECKS:
                break

        # 2. Nothing passed (no hits, or every candidate rejected): page through the Library.
        for page in range(self.LIBRARY_PAGES):
            out = await server.ss_list_media_items(index=page * self.LIBRARY_PAGE, count=self.LIBRARY_PAGE, brief=True)
            err = self._err("ss_list_media_items", out)
            if err:
                errs.append(err)
                break
            items = as_list(result_of(out))
            for it in items:
                if not isinstance(it, dict) or not usable_guid(it.get("Id")) or it["Id"].lower() in seen:
                    continue
                if it.get("InProgress") or it.get("Clip"):
                    continue
                if checked["library"] >= self.MAX_CHECKS:
                    break
                seen.add(it["Id"].lower())
                checked["library"] += 1
                item = await self._check_media_item(it["Id"], rejected, errs)
                if item is not None:
                    return item
            if len(items) < self.LIBRARY_PAGE or checked["library"] >= self.MAX_CHECKS:
                break

        self._miss("media_item", (
            f"no completed, non-clip media item of at least {self.MIN_SOURCE_SECONDS:.0f} s with captions "
            f"among {checked['search']} caption-search and {checked['library']} Library candidates "
            f"(rejected: {rejected})"
        ), errs)
        return None

    # --- channel, airing, job, task list, drive, bookmark
    def channel_id(self) -> Optional[str]:
        return self._get("channel_id", self._find_channel)

    async def _find_channel(self) -> Optional[str]:
        errs: List[str] = []
        for label, call in (("ss_list_channels", lambda: server.ss_list_channels(count=25)),
                            ("ss_list_library_channels", server.ss_list_library_channels)):
            out = await call()
            err = self._err(label, out)
            if err:
                errs.append(err)
                continue
            cid = first_id(as_list(result_of(out)))
            if cid is not None:
                return cid
        self._miss("channel_id", "Scheduler/GetChannels and Library/GetAllPastAndCurrentChannels returned no channel id", errs)
        return None

    def airing(self) -> Optional[Dict[str, Any]]:
        return self._get("airing", self._find_airing)

    async def _find_airing(self) -> Optional[Dict[str, Any]]:
        cid = await self._aget("channel_id", self._find_channel)
        errs: List[str] = []
        calls = []
        if cid:
            calls.append(("ss_guide_for_channel", lambda: server.ss_guide_for_channel(cid, count=5)))
        calls.append(("ss_guide_by_time", lambda: server.ss_guide_by_time(count=5)))
        for label, call in calls:
            out = await call()
            err = self._err(label, out)
            if err:
                errs.append(err)
                continue
            for it in as_list(result_of(out)):
                if isinstance(it, dict) and usable_guid(it.get("Id")):
                    return it
        self._miss("airing", "no airing in the guide for the next hours (by channel or by start time)", errs)
        return None

    def job_id(self) -> Optional[str]:
        return self._get("job_id", self._find_job)

    async def _find_job(self) -> Optional[str]:
        out = await server.ss_list_recording_jobs(count=5)
        err = self._err("ss_list_recording_jobs", out)
        jid = None if err else first_id(as_list(result_of(out)))
        if jid is None:
            self._miss("job_id", "Scheduler/GetRecordingJobs returned no job", [err] if err else [])
        return jid

    def task_list_id(self) -> Optional[str]:
        return self._get("task_list_id", self._find_task_list)

    async def _find_task_list(self) -> Optional[str]:
        out = await server.ss_call_read("TaskManager", "GetTaskLists", {"index": 0, "count": 5})
        err = self._err("TaskManager/GetTaskLists", out)
        tid = None if err else first_id(as_list(result_of(out)))
        if tid is None:
            self._miss("task_list_id", "TaskManager/GetTaskLists returned no task list", [err] if err else [])
        return tid

    def drive(self) -> Optional[Dict[str, Any]]:
        return self._get("drive", self._find_drive)

    async def _find_drive(self) -> Optional[Dict[str, Any]]:
        out = await server.ss_storage_status()
        err = self._err("ss_storage_status", out)
        if not err:
            for it in as_list(result_of(out)):
                if isinstance(it, dict) and usable_guid(it.get("Id")):
                    return it
        self._miss("drive", "Library/GetDrives returned no drive", [err] if err else [])
        return None

    def bookmark_id(self) -> Optional[str]:
        return self._get("bookmark_id", self._find_bookmark)

    async def _find_bookmark(self) -> Optional[str]:
        item = await self._aget("media_item", self._find_media_item)
        if item is None:
            self.reasons["bookmark_id"] = "no media item fixture"
            return None
        out = await server.ss_list_clips_and_bookmarks(item["Id"], count=5)
        err = self._err("ss_list_clips_and_bookmarks", out)
        bid = None if err else first_id(as_list(result_of(out)))
        if bid is None:
            self._miss("bookmark_id", "the fixture media item has no clips or bookmarks", [err] if err else [])
        return bid

    # --- ids for the catalog sweep
    def sweep_ids(self) -> Dict[str, Optional[str]]:
        item = self.media_item() or {}
        airing = self.airing() or {}
        drive = self.drive() or {}

        def pick(*vals: Any) -> Optional[str]:
            for v in vals:
                if usable_guid(v):
                    return v
            return None

        return {
            "media_item": pick(item.get("Id")),
            "channel": pick(self.channel_id(), item.get("ChannelId")),
            "airing": pick(airing.get("Id")),
            "job": pick(self.job_id(), airing.get("RecordingJobId"), item.get("JobId")),
            "task_list": pick(self.task_list_id()),
            "bookmark": pick(self.bookmark_id()),
            "folder": pick(item.get("FolderId")),
            "folder_path": pick(item.get("FolderPathId")),
            "drive": pick(item.get("DriveId"), drive.get("Id")),
            "machine": pick(drive.get("MachineId"), item.get("FolderPathMachineId")),
            "series": pick(airing.get("SeriesId"), item.get("SeriesId")),
            "episode": pick(airing.get("EpisodeId"), item.get("EpisodeId")),
            "movie": pick(airing.get("MovieId"), item.get("MovieId")),
            "recording": pick(item.get("RecordingId"), airing.get("RecordingId")),
            "context": pick(item.get("ContextId")),
        }


# ------------------------------------------------------------- catalog sweep params
# Parameter name (lower case) -> fixture id. "id" and "itemId" first look at the method name.
GUID_NAME_MAP = {
    "itemid": "media_item", "mediaitem": "media_item", "mediaitemid": "media_item", "id": "media_item",
    "parentid": "media_item", "parentitemid": "media_item",
    "channelid": "channel",
    "airing": "airing", "airingid": "airing",
    "jobid": "job", "recordingjobid": "job",
    "listid": "task_list", "tasklistid": "task_list",
    "folderid": "folder", "folderpathid": "folder_path", "pathid": "folder_path",
    "driveid": "drive", "machineid": "machine",
    "seriesid": "series", "episodeid": "episode", "movieid": "movie",
    "recordingid": "recording", "contextid": "context",
}
METHOD_ID_HINTS = (
    ("Bookmark", "bookmark"), ("Airing", "airing"), ("RecordingJob", "job"), ("Episode", "episode"),
    ("Movie", "movie"), ("Series", "series"), ("TaskList", "task_list"),
)
GUID_LIST_MAP = {"channelids": "channel", "channellist": "channel", "airingids": "airing", "itemids": "media_item"}
INT_TYPES = ("int", "long", "uint", "short", "byte")
NULL_TYPES = ("ColumnFilter[]", "ColumnSort[]", "ColumnFilter[][]", "ColumnSelect[]")


def _date_for(name: str, now: _dt.datetime) -> str:
    n = name.lower()
    if n.endswith(("end", "endutc", "endtime")) or n in ("to", "until"):
        return iso(now)
    if any(x in n for x in ("begin", "start", "from")):
        return iso(now - _dt.timedelta(hours=24))
    return iso(now)


def build_sweep_params(info: MethodInfo, ids: Dict[str, Optional[str]], catalog: Catalog,
                       now: Optional[_dt.datetime] = None) -> Tuple[Dict[str, Any], List[str]]:
    """Params for one read method from its schema. Returns (params, names given a synthetic GUID)."""
    now = now or utc_now()
    params: Dict[str, Any] = {}
    synthetic: List[str] = []
    for name, typ in info.params:
        n = name.lower()
        if typ == "Guid":
            src = None
            if n in ("id", "itemid"):
                src = next((f for hint, f in METHOD_ID_HINTS if hint in info.name), None)
            src = src or GUID_NAME_MAP.get(n)
            val = ids.get(src) if src else None
            if not usable_guid(val):
                val = str(uuid.uuid4())
                synthetic.append(name)
            params[name] = val
        elif typ == "Guid[]":
            src = GUID_LIST_MAP.get(n)
            val = ids.get(src) if src else None
            params[name] = [val] if usable_guid(val) else []
        elif typ in INT_TYPES:
            params[name] = 1 if n == "count" else 0
        elif typ == "double":
            params[name] = 0.0
        elif typ == "bool":
            params[name] = False
        elif typ in ("string", "String"):
            params[name] = ""
        elif typ == "string[]":
            params[name] = []
        elif typ == "DateTime":
            params[name] = _date_for(name, now)
        elif typ in NULL_TYPES:
            params[name] = None
        else:
            values = catalog.enum_values(typ)
            params[name] = next(iter(values.values())) if values else None
    return params, synthetic


SWEEP_FAIL_OUTCOMES = ("http_404", "transport", "local_crash", "secret_leak", "auth", "config", "budget")


def classify(out: Any) -> Tuple[str, Optional[int], str]:
    """(outcome, http status, short message) for one ss_call_read result."""
    if not isinstance(out, dict):
        return "local_crash", None, f"returned {type(out).__name__}"
    if "error" not in out:
        return "ok", None, ""
    kind = out.get("kind")
    status = out.get("http_status")
    msg = short(out.get("error"))
    if kind == "api_fault":
        return "api_fault", status, msg
    if kind == "http":
        return ("http_404" if status == 404 else "http"), status, msg
    if kind in ("timeout", "network"):
        return "transport", status, msg
    if kind in ("auth", "config", "budget"):
        return kind, status, msg
    if str(out.get("error", "")).startswith("Unexpected error"):
        return "local_crash", status, msg
    return "local_refusal", status, msg


# ------------------------------------------------------------------------- report
class LiveReport:
    """Collected during the run and written once at session end as .md and .json. Holds only
    method names, outcomes, statuses, short messages, latencies, ids this run created, and test
    outcomes: never response bodies."""

    def __init__(self, base: Path, meta: Dict[str, Any]) -> None:
        self.base = base
        self.meta = dict(meta)
        self.tests: List[Dict[str, Any]] = []
        self.sweep: List[Dict[str, Any]] = []
        self.sweep_skipped: List[Dict[str, str]] = []
        self.sweep_notes: List[str] = []
        self.rate: Dict[str, Any] = {}
        self.writes: List[Dict[str, Any]] = []
        self.cleanup: List[Dict[str, Any]] = []
        self.fixtures: Dict[str, Any] = {}

    @property
    def md_path(self) -> Path:
        return self.base.with_suffix(".md")

    @property
    def json_path(self) -> Path:
        return self.base.with_suffix(".json")

    def data(self) -> Dict[str, Any]:
        counts: Dict[str, int] = {}
        for r in self.sweep:
            counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
        tcounts: Dict[str, int] = {}
        for t in self.tests:
            tcounts[t["outcome"]] = tcounts.get(t["outcome"], 0) + 1
        return {
            "meta": self.meta,
            "test_counts": tcounts,
            "tests": self.tests,
            "fixtures": self.fixtures,
            "sweep_counts": counts,
            "sweep": self.sweep,
            "sweep_skipped_not_read_only": self.sweep_skipped,
            "sweep_notes": self.sweep_notes,
            "rate_burst": self.rate,
            "writes": self.writes,
            "cleanup": self.cleanup,
        }

    def write(self) -> None:
        self.base.parent.mkdir(parents=True, exist_ok=True)
        d = self.data()
        self.json_path.write_text(json.dumps(d, indent=2, default=str), encoding="utf-8")
        self.md_path.write_text(self.markdown(d), encoding="utf-8")

    @staticmethod
    def _cell(v: Any) -> str:
        return str("" if v is None else v).replace("|", "/").replace("\n", " ")

    def markdown(self, d: Dict[str, Any]) -> str:
        c = self._cell
        m = d["meta"]
        out = [
            "# SnapStream live test report",
            "",
            f"- Started (UTC): {m.get('started')}",
            f"- Finished (UTC): {m.get('finished')}",
            f"- Host: {m.get('host')}",
            f"- Tiers: read{' + write' if m.get('writes') else ''}" + (f", orientation {m.get('orientation')}" if m.get("orientation") is not None else ""),
            f"- Run stamp: {m.get('run_stamp')}",
            "",
            "## Tests",
            "",
            "Outcomes: " + ", ".join(f"{k} {v}" for k, v in sorted(d["test_counts"].items())),
            "",
            "| Test | Outcome | Note |",
            "|---|---|---|",
        ]
        out += [f"| {c(t['nodeid'])} | {c(t['outcome'])} | {c(t.get('note'))} |" for t in d["tests"]]
        if d["fixtures"]:
            out += ["", "## Discovery fixtures", ""]
            out += [f"- {k}: {c(v)}" for k, v in sorted(d["fixtures"].items())]
        out += ["", "## Catalog sweep", ""]
        out.append("Outcomes: " + (", ".join(f"{k} {v}" for k, v in sorted(d["sweep_counts"].items())) or "not run"))
        out += [f"- {c(n)}" for n in d["sweep_notes"]]
        fails = [r for r in d["sweep"] if r["outcome"] in SWEEP_FAIL_OUTCOMES]
        if fails:
            out += ["", "### Failures", ""]
            out += [f"- {c(r['method'])}: {c(r['outcome'])} {c(r.get('http_status'))} {c(r.get('message'))}" for r in fails]
        if d["sweep"]:
            out += ["", "| Method | Outcome | HTTP | Message | ms |", "|---|---|---|---|---|"]
            out += [
                f"| {c(r['method'])} | {c(r['outcome'])} | {c(r.get('http_status'))} | {c(r.get('message'))} | {c(r.get('latency_ms'))} |"
                for r in d["sweep"]
            ]
        if d["sweep_skipped_not_read_only"]:
            out += ["", "### Skipped (access read, spec read-only false)", ""]
            out += [f"- {c(s['method'])}" for s in d["sweep_skipped_not_read_only"]]
        if d["rate_burst"]:
            out += ["", "## Rate burst", ""]
            out += [f"- {k}: {c(v)}" for k, v in d["rate_burst"].items()]
        if m.get("writes"):
            out += ["", "## Write tier", ""]
            if not d["writes"]:
                out.append("No writes were committed.")
            for w in d["writes"]:
                out.append("- " + ", ".join(f"{k}: {c(v)}" for k, v in w.items()))
            out += ["", "### Cleanup", ""]
            if not d["cleanup"]:
                out.append("Nothing to clean up.")
            for r in d["cleanup"]:
                out.append("- " + ", ".join(f"{k}: {c(v)}" for k, v in r.items()))
        out.append("")
        return "\n".join(out)


def timed() -> Callable[[], int]:
    t0 = time.perf_counter()
    return lambda: int(round((time.perf_counter() - t0) * 1000))
