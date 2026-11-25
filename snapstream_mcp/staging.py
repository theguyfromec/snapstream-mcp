"""Two-phase write gate: stage -> (user approval) -> commit.

ss_stage_write validates the method (access must be exactly "write"; read and blocked are
refused), validates the parameters against the catalog, freezes the exact request payload as
canonical JSON with a SHA-256 hash, and returns a single-use token that expires after 10 minutes
together with a human-readable preview.

ss_commit_write consumes the token (on any attempt), re-checks the hash, and sends exactly the
frozen payload once through the client's write throttle. Every step is appended to a JSONL audit
file. The audit never contains credentials, tokens, or credential-looking parameter values.
Every audit line about a staged write carries its stage_id (a short hash of the token, so the
token itself is never logged), method and payload_sha256, so each line joins to its "staged" line.
Write methods that may carry destination or account credentials (export destinations, task
actions, live streaming, federation) are masked with the strict field rules in previews and audit.

At stage time the keys of complex parameters are checked against the API schemas (unknown keys and
case-only mismatches are refused, never auto-corrected) and ClipProperties tick positions are
checked (integers, start before end, at least 1 s).

At commit, a fault is never reported as committed. A timeout or a network failure after the
request was sent, HTTP 502/503/504, or an interruption (cancellation) while the request was in
flight returns status "outcome_unknown" with a verify_hint naming the read that confirms the
result; writes are never retried automatically. A fault from the method itself or an HTTP 5xx is
status "error" with a verify_hint, because part of the write may have run; only failures known to
come before the method ran (gateway permission inspector, HTTP 4xx, connect failures) say "Not
committed". A RecordAiring "already scheduled" fault is status "already_done". A write whose
catalog return type is Guid must answer with a non-empty GUID, or it is reported as an error.

The frozen payload keeps the caller's key order with every "__type" moved to the front of its
object (WCF needs the subtype hint first); the SHA-256 is over the key-sorted form.
"""

from __future__ import annotations

import asyncio
import copy
import datetime
import hashlib
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .catalog import Catalog, check_clip_positions, coerce_params, missing_note
from .client import Redactor, SnapStreamClient, SnapStreamError, clamp_count
from .formatting import EMPTY_GUID, STRICT_REDACT_WRITE_METHODS, is_guid, redact_fields, unwrap_result

STAGE_TTL_SECONDS = 600
MAX_PENDING = 25
MAX_PAYLOAD_CHARS = 100_000
OUTCOME_UNKNOWN_STATUSES = (502, 503, 504)
IDENTITY_LINE = "Runs as:     the single SnapStream user mapped to this API client ID (every API call does)."


# TaskManager writes that create a task list (their Guid result is the task list id).
TASK_LIST_METHODS = frozenset((
    "AiClipDesigner", "BatchShareClip", "BatchUploadClipsToYouTube", "ClipAndBlueskyPost",
    "ClipAndFacebookPost", "ClipAndSlack", "ClipAndSlackGIF", "ClipAndTweet", "ClipAndUpload", "ClipFile",
    "CopyFile", "CreateGifFromExistingClip", "CreateScreenshot", "DeleteFile", "DesignExistingClip",
    "DetectCommercials", "EmailClip", "ExportFile", "FederatedDownload", "FederatedPlaylistDownload",
    "FederatedPlaylistUpload", "FederatedUpload", "MergeFiles", "MoveFile", "PostClipToFacebook",
    "PostExistingClipToBluesky", "PostExistingMoment", "RemuxFile", "RunTaskRule", "ShareClip",
    "ShowSqueezeFile", "SlackClip", "SlackClipAsGIF", "SlackExistingGIF", "SlackImage", "StreamToFacebookLive",
    "StreamToYouTubeLive", "TagFile", "TranscribeRecording", "TrimFile", "TweetClip", "TweetClipAsGIF",
    "TweetExistingGIF", "TweetScreenshot", "UpdateDesign", "UploadClip",
))
RECORDING_METHODS = frozenset(("RecordAiring", "RecordShow"))
ALREADY_SCHEDULED_RE = re.compile(r"already\s+(?:been\s+)?scheduled", re.I)


def verify_hint(method: str) -> str:
    """The read that tells whether a write whose outcome is unknown actually happened."""
    if method in TASK_LIST_METHODS:
        return (
            "Check with TaskManager/GetTaskLists (ss_call_read) or ss_get_job for a task list created by "
            "this write before staging it again."
        )
    if method in RECORDING_METHODS:
        return (
            "Check with Scheduler/GetRecordingJobs or Scheduler/GetCurrentAndUpcomingRecordings "
            "(ss_list_scheduled_recordings) for the recording before staging it again."
        )
    if method == "AddAlert":
        return "Check with NotifierManager/GetMyAlerts (ss_call_read) for the alert before staging it again."
    return "Re-read the object before retrying."


def is_definite_failure(exc: SnapStreamError) -> bool:
    """True when the write certainly did not run: the gateway's permission inspector rejected it,
    the gateway or WCF refused it with HTTP 4xx, or it never left this machine. A fault from the
    method itself, or an HTTP 5xx, may come after part of the write ran."""
    if exc.before_execution:
        return True
    if exc.kind == "api_fault":
        return False
    if exc.kind == "http":
        return exc.status is not None and exc.status < 500
    return True


def is_outcome_unknown(exc: SnapStreamError) -> bool:
    if exc.kind in ("timeout", "network") and exc.sent is not False:
        return True
    return exc.kind == "http" and exc.status in OUTCOME_UNKNOWN_STATUSES


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def type_first(obj: Any) -> Any:
    """Copy with "__type" moved to the front of every object (other keys keep their order).
    WCF's DataContractJsonSerializer only honors a subtype hint that is the first member."""
    if isinstance(obj, dict):
        items = [(k, type_first(v)) for k, v in obj.items()]
        return dict(sorted(items, key=lambda kv: kv[0] != "__type"))
    if isinstance(obj, list):
        return [type_first(v) for v in obj]
    return obj


def wire_json(obj: Any) -> str:
    """The frozen payload as sent: caller key order, "__type" first in every object."""
    return json.dumps(type_first(obj), separators=(",", ":"), ensure_ascii=False)


def _display(obj: Any) -> Any:
    """Keys sorted for the preview, with "__type" first as it is sent."""
    if isinstance(obj, dict):
        return type_first({k: _display(obj[k]) for k in sorted(obj)})
    if isinstance(obj, list):
        return [_display(v) for v in obj]
    return obj


def payload_hash(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def stage_id_for(token: str) -> str:
    """Audit-safe identifier for a staging token: the first 16 hex digits of its SHA-256."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


@dataclass
class StagedWrite:
    token: str
    service: str
    method: str
    payload_json: str
    payload_sha256: str
    created: float
    expires: float
    reason: str
    summary: str
    preview: str
    notes: List[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.service}/{self.method}"

    @property
    def stage_id(self) -> str:
        return stage_id_for(self.token)


class WriteStager:
    def __init__(
        self,
        catalog: Catalog,
        audit_file: Path,
        *,
        ttl: int = STAGE_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
        redactor: Optional[Redactor] = None,
        empty_auth_ticket: bool = True,
    ) -> None:
        self.catalog = catalog
        self.empty_auth_ticket = empty_auth_ticket
        self.audit_file = Path(audit_file)
        self.ttl = ttl
        self._clock = clock
        self.redact = redactor or Redactor()
        self._staged: Dict[str, StagedWrite] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ audit
    def audit(self, event: str, **fields: Any) -> bool:
        """Append one redacted JSON line. Returns False when the file could not be written."""
        record = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "event": event,
            **redact_fields(fields),
        }
        line = self.redact(json.dumps(record, ensure_ascii=False, default=str))
        try:
            self.audit_file.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_file.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            return True
        except OSError:
            return False

    def audit_write(self, event: str, sw: "StagedWrite", **fields: Any) -> bool:
        """Audit an event about one staged write, tagged so it joins to its "staged" line."""
        return self.audit(event, stage_id=sw.stage_id, method=sw.key, payload_sha256=sw.payload_sha256, **fields)

    # ---------------------------------------------------------------- helpers
    def _purge_expired(self) -> None:
        now = self._clock()
        with self._lock:
            for tok in [t for t, s in self._staged.items() if s.expires <= now]:
                self._staged.pop(tok, None)

    def _refusal(self, message: str, **extra: Any) -> Dict[str, Any]:
        return {"status": "refused", "error": message, **extra}

    # ------------------------------------------------------------------ stage
    def stage(
        self,
        service: str,
        method: str,
        params: Optional[Dict[str, Any]],
        reason: str = "",
        *,
        extra_preview: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        self._purge_expired()
        info = self.catalog.get(service, method)
        if info is None:
            return self._refusal(
                f"Unknown method {service}/{method}. Use ss_api_catalog to find the exact service and method name.",
                suggestions=self.catalog.suggest(service, method),
            )
        if info.access == "blocked":
            self.audit("stage_refused_blocked", method=info.key)
            return self._refusal(
                f"{info.key} is blocked and can never be called through this server: {info.access_reason}"
            )
        if info.access != "write":
            return self._refusal(
                f"{info.key} is a read method. Call it with ss_call_read (or a curated ss_ tool); staging is only for writes."
            )
        try:
            body, notes = coerce_params(info, copy.deepcopy(params) if params is not None else None)
            body = clamp_count(body)
        except (ValueError, SnapStreamError) as exc:
            return self._refusal(f"Invalid parameters: {exc}", valid_params=[f"{n}: {t}" for n, t in info.params])
        problems: List[str] = []
        clip_lines: List[str] = []
        for pname, ptype in info.params:
            if pname not in body:
                continue
            problems += self.catalog.validate_nested(ptype, body[pname], pname)
            if ptype == "ClipProperties" and isinstance(body[pname], dict):
                err, lines = check_clip_positions(info.name, body[pname])
                if err:
                    problems.append(err)
                clip_lines += lines
        if problems:
            return self._refusal(
                "Invalid parameters: " + " | ".join(problems),
                hint="Use ss_api_describe(service, method) for the exact field names and types.",
            )
        mn = missing_note(info, body)
        if mn:
            notes.append(mn)
        if body.get("waitForRebuild") is True:
            notes.append(
                "waitForRebuild is true: the call waits for the schedule rebuild (up to about 25 s), close to "
                "the gateway's 29 s limit. A timeout then reports outcome_unknown; check before staging again."
            )
        payload_json = wire_json(body)
        if len(payload_json) > MAX_PAYLOAD_CHARS:
            return self._refusal(f"Payload is {len(payload_json)} characters; the limit is {MAX_PAYLOAD_CHARS}.")
        with self._lock:
            if len(self._staged) >= MAX_PENDING:
                return self._refusal(
                    f"{MAX_PENDING} writes are already pending. Commit or cancel some (ss_pending_writes, ss_cancel_staged) first."
                )
        reason = (reason or "").strip()[:500]
        sha = payload_hash(body)
        token = "ssw_" + secrets.token_urlsafe(12)
        now = self._clock()
        shown = redact_fields(body, strict=bool(STRICT_REDACT_WRITE_METHODS.search(info.name)))
        preview_lines = [
            "SNAPSTREAM WRITE (staged, NOT executed)",
            f"Method:      {info.key}  [access: write]" + (f"  (web app: {info.ui_name})" if info.ui_name else ""),
            f"What it does: {info.summary}",
            f"Risk note:   {info.access_reason}",
            f"Permissions: {', '.join(info.perms) or 'none listed'}",
            IDENTITY_LINE,
            f"Reason:      {reason or '(none given)'}",
        ]
        preview_lines += list(extra_preview or [])
        preview_lines += clip_lines
        preview_lines += [
            "Parameters (sent exactly as shown; credential-looking values masked here only):",
            json.dumps(_display(shown), indent=2, ensure_ascii=False),
        ]
        if info.auth_ticket == "empty_object":
            if self.empty_auth_ticket:
                notes.append('"authTicket": {} is added when sending (the gateway needs the stub for this method on older builds).')
            else:
                notes.append("authTicket is omitted (SNAPSTREAM_MCP_EMPTY_AUTH_TICKET is off); older builds may fail this method.")
        if info.note:
            notes.append(f"API note: {info.note}")
        preview_lines += [f"Note: {n}" for n in notes]
        preview_lines += [
            f"Payload SHA-256: {sha}",
            f"Single-use token {token}, expires in {self.ttl // 60} minutes.",
        ]
        preview = self.redact("\n".join(preview_lines))
        sw = StagedWrite(
            token=token,
            service=info.service,
            method=info.name,
            payload_json=payload_json,
            payload_sha256=sha,
            created=now,
            expires=now + self.ttl,
            reason=reason,
            summary=info.summary,
            preview=preview,
            notes=notes,
        )
        with self._lock:
            self._staged[token] = sw
        audited = self.audit_write("staged", sw, params=shown, reason=reason)
        warnings = [] if audited else [f"Audit log {self.audit_file.name} is not writable; commit will be refused until it is."]
        return {
            "status": "staged",
            "token": token,
            "stage_id": sw.stage_id,
            "method": info.key,
            "expires_in_seconds": self.ttl,
            "payload_sha256": sha,
            "preview": preview,
            "warnings": warnings,
            "next_step": (
                "Show this preview to the user and get explicit approval. Then call ss_commit_write(token). "
                "The commit sends exactly this payload once. To discard it, call ss_cancel_staged(token)."
            ),
        }

    # ----------------------------------------------------------------- commit
    def take(self, token: str) -> Tuple[Optional[StagedWrite], Optional[Dict[str, Any]]]:
        """Remove and return a live staged write. Any attempt consumes the token."""
        token = (token or "").strip()
        with self._lock:
            sw = self._staged.pop(token, None)
        if sw is None:
            return None, self._refusal("No staged write for this token (already used, canceled, expired, or never staged). Stage it again.")
        if sw.expires <= self._clock():
            self.audit_write("commit_refused_expired", sw)
            return None, self._refusal("This staged write has expired (10 minute limit). Stage it again.")
        return sw, None

    async def execute(self, sw: StagedWrite, client: SnapStreamClient, *, elicit_mode: str = "") -> Dict[str, Any]:
        """Send the frozen payload once. elicit_mode records how the user confirmed it."""
        body = json.loads(sw.payload_json)
        if payload_hash(body) != sw.payload_sha256:
            self.audit_write("commit_refused_integrity", sw)
            return self._refusal("Payload integrity check failed; nothing was sent. Stage the write again.")
        info = self.catalog.get(sw.service, sw.method)
        if info is None or info.access != "write":
            self.audit_write("commit_refused_access", sw)
            return self._refusal(f"{sw.key} is no longer classified as write; nothing was sent.")
        if not self.audit_write("commit_start", sw, elicit_mode=elicit_mode or "none"):
            return self._refusal(f"Audit log {self.audit_file.name} is not writable; nothing was sent.")
        meta: Dict[str, Any] = {}
        hint = verify_hint(sw.method)
        try:
            raw = await client.call(sw.service, sw.method, body, write=True, meta=meta)
        except SnapStreamError as exc:
            if is_outcome_unknown(exc):
                self.audit_write(
                    "commit_unknown", sw, error=exc.message, kind=exc.kind, http_status=exc.status, request_id=exc.request_id
                )
                return {
                    "status": "outcome_unknown",
                    "method": sw.key,
                    **exc.to_dict(),
                    "verify_hint": hint,
                    "note": f"The request may or may not have run; it was NOT retried. The token is spent. {hint}",
                }
            if exc.kind == "api_fault" and sw.method in RECORDING_METHODS and ALREADY_SCHEDULED_RE.search(exc.message):
                self.audit_write(
                    "commit_already_done", sw, error=exc.message, kind=exc.kind, http_status=exc.status, request_id=exc.request_id
                )
                return {
                    "status": "already_done",
                    "method": sw.key,
                    **exc.to_dict(),
                    "verify_hint": hint,
                    "note": f"The server says this recording is already scheduled, so nothing new was added. {hint}",
                }
            self.audit_write(
                "commit_error", sw, error=exc.message, kind=exc.kind, http_status=exc.status, request_id=exc.request_id
            )
            if is_definite_failure(exc):
                return {"status": "error", "method": sw.key, **exc.to_dict(), "note": "Not committed. The token is spent. Stage again to retry."}
            return {
                "status": "error",
                "method": sw.key,
                **exc.to_dict(),
                "verify_hint": hint,
                "note": f"The server reported a failure, but part of the write may have run; it was NOT retried. The token is spent. {hint}",
            }
        except BaseException as exc:
            # Cancellation (the MCP client gave up) or an unexpected error while the request may be
            # in flight: never leave commit_start without a closing audit line.
            kind = "canceled" if isinstance(exc, asyncio.CancelledError) else type(exc).__name__
            error = self.redact(f"{type(exc).__name__}: {exc}")[:300]
            self.audit_write("commit_unknown", sw, error=error, kind=kind, verify_hint=hint)
            if not isinstance(exc, Exception):
                raise  # CancelledError, KeyboardInterrupt, SystemExit, GeneratorExit
            return {
                "status": "outcome_unknown",
                "method": sw.key,
                "error": error,
                "kind": kind,
                "verify_hint": hint,
                "note": f"The request may or may not have run; it was NOT retried. The token is spent. {hint}",
            }
        if info.returns == "Guid":
            value = unwrap_result(raw, sw.method)
            if not is_guid(value) or value == EMPTY_GUID:
                shown = self.redact(json.dumps(raw, ensure_ascii=False, default=str))
                shown = shown[:200] + ("..." if len(shown) > 200 else "")
                error = f"{sw.key} answered without a task GUID (got {shown}), so the write is not confirmed."
                self.audit_write(
                    "commit_error", sw, error=error, kind="unexpected_result", http_status=None, request_id=meta.get("request_id")
                )
                out = {
                    "status": "error",
                    "method": sw.key,
                    "error": error,
                    "kind": "unexpected_result",
                    "verify_hint": hint,
                    "note": f"Not confirmed; it was NOT retried. The token is spent. {hint}",
                }
                if meta.get("request_id"):
                    out["request_id"] = meta["request_id"]
                return out
        self.audit_write("commit_done", sw, elicit_mode=elicit_mode or "none", request_id=meta.get("request_id"))
        out = {"status": "committed", "method": sw.key, "payload_sha256": sw.payload_sha256, "raw": raw}
        if meta.get("request_id"):
            out["request_id"] = meta["request_id"]
        return out

    # ------------------------------------------------------------ housekeeping
    def cancel(self, token: str) -> Dict[str, Any]:
        token = (token or "").strip()
        with self._lock:
            sw = self._staged.pop(token, None)
        if sw is None:
            return self._refusal("No staged write for this token (already used, canceled, or expired).")
        self.audit_write("canceled", sw)
        return {"status": "canceled", "token": token, "stage_id": sw.stage_id, "method": sw.key}

    def pending(self) -> Dict[str, Any]:
        self._purge_expired()
        now = self._clock()
        with self._lock:
            items = [
                {
                    "token": s.token,
                    "stage_id": s.stage_id,
                    "method": s.key,
                    "reason": s.reason,
                    "payload_sha256": s.payload_sha256,
                    "expires_in_seconds": int(max(0, s.expires - now)),
                }
                for s in sorted(self._staged.values(), key=lambda x: x.created)
            ]
        return {"count": len(items), "pending": items}
