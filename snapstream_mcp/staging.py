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
"""

from __future__ import annotations

import copy
import datetime
import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .catalog import Catalog, coerce_params, missing_note
from .client import Redactor, SnapStreamClient, SnapStreamError, clamp_count
from .formatting import STRICT_REDACT_WRITE_METHODS, redact_fields

STAGE_TTL_SECONDS = 600
MAX_PENDING = 25
MAX_PAYLOAD_CHARS = 100_000


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


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
    ) -> None:
        self.catalog = catalog
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
    def stage(self, service: str, method: str, params: Optional[Dict[str, Any]], reason: str = "") -> Dict[str, Any]:
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
        mn = missing_note(info, body)
        if mn:
            notes.append(mn)
        payload_json = canonical_json(body)
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
            f"Method:      {info.key}  [access: write]",
            f"What it does: {info.summary}",
            f"Risk note:   {info.access_reason}",
            f"Permissions: {', '.join(info.perms) or 'none listed'}",
            f"Reason:      {reason or '(none given)'}",
            "Parameters (sent exactly as shown; credential-looking values masked here only):",
            json.dumps(shown, indent=2, ensure_ascii=False, sort_keys=True),
        ]
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
        try:
            raw = await client.call(sw.service, sw.method, body, write=True)
        except SnapStreamError as exc:
            self.audit_write("commit_error", sw, error=exc.message, http_status=exc.status)
            return {"status": "error", "method": sw.key, **exc.to_dict(), "note": "The token is spent. Stage again to retry."}
        self.audit_write("commit_done", sw, elicit_mode=elicit_mode or "none")
        return {"status": "committed", "method": sw.key, "payload_sha256": sw.payload_sha256, "raw": raw}

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
