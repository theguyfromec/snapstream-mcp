"""Async HTTP client for the SnapStream tenant API.

* Access tokens come from the Cognito token endpoint (client_credentials). The token is cached
  and refreshed 60 seconds before it expires; a 401 from the API triggers one refresh and retry.
* Every call is `POST {base_url}/{url_path}/json/{Method}` with a JSON body keyed by parameter
  name. url_path is the gateway path segment from the catalog (Config, LocalConfig, Notifier; the
  other services use their name). Any caller `authTicket` key is removed; methods whose catalog
  auth_ticket is "empty_object" are sent with `"authTicket": {}` while
  SNAPSTREAM_MCP_EMPTY_AUTH_TICKET is on (the default), because the gateway inspector throws
  without it on builds before the fix.
* `count` is clamped to 1..1000. Writes are serialized and spaced by a minimum interval, a
  semaphore caps concurrent requests, and a token bucket (default 8 per second, burst 16) paces
  every API request. A soft daily budget (UTC day, counted in a file beside the audit log)
  refuses calls once it is spent.
* Reads that get HTTP 429 or 503 are retried (Retry-After, or jittered backoff) up to 3 attempts
  in all. Writes are never retried.
* The API reports many failures as HTTP 200 with a base64 `CustomError` header, or as an XML,
  HTML or WCF fault body. Those raise SnapStreamError(kind="api_fault") with the first line of the
  message (stack frames stripped), never a success.
* Errors carry the HTTP status, the gateway's x-amzn-RequestId and a short body excerpt. Every
  error string passes through the Redactor, which removes credentials and tokens.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import datetime
import html
import json
import random
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional

import httpx

from .config import Config, Settings

MAX_COUNT = 1000
DEFAULT_COUNT = 50
TOKEN_REFRESH_MARGIN = 60.0
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,99}$")
REDACTED = "[REDACTED]"
# Fallback when no catalog is attached: display name -> gateway path segment.
SERVICE_URL_PATHS = {"Configuration": "Config", "LocalConfiguration": "LocalConfig", "NotifierManager": "Notifier"}
RETRY_READ_STATUSES = (429, 503)
MAX_RETRY_AFTER = 30.0
FAULT_MESSAGE_LIMIT = 200
REQUEST_ID_HEADER = "x-amzn-RequestId"
CUSTOM_ERROR_HEADER = "CustomError"


class SnapStreamError(Exception):
    """An API, auth or transport failure. The message is already redacted.

    sent is True when the request may have reached the server (a read timeout or a connection
    dropped after sending), False when it certainly did not, None when not applicable.
    before_execution is True for a fault raised by the gateway's permission inspector, which runs
    before the method itself, so nothing was changed."""

    def __init__(
        self,
        message: str,
        *,
        kind: str = "api",
        status: Optional[int] = None,
        service: Optional[str] = None,
        method: Optional[str] = None,
        excerpt: Optional[str] = None,
        request_id: Optional[str] = None,
        sent: Optional[bool] = None,
        before_execution: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.status = status
        self.service = service
        self.method = method
        self.excerpt = excerpt
        self.before_execution = before_execution
        self.request_id = request_id
        self.sent = sent

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"error": self.message, "kind": self.kind}
        if self.status is not None:
            out["http_status"] = self.status
        if self.service and self.method:
            out["method"] = f"{self.service}/{self.method}"
        if self.excerpt:
            out["body_excerpt"] = self.excerpt
        if self.request_id:
            out["request_id"] = self.request_id
        return out


class Redactor:
    """Masks known secret values plus anything that looks like a bearer token, a JWT, a credential
    key/value pair, or a credential in a URL query string (token, sig, signature, key, X-Amz-*).

    __call__ is for error and log text. scrub() is for API response data: the same masking, except
    that "bearer" must be followed by a token-like run of 16 or more characters, so ordinary prose
    such as "the bearer of bad news" in a transcript is left alone."""

    _JWT = (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"), REDACTED)
    _KV = (
        re.compile(r'(?i)("?(?:access_token|id_token|refresh_token|client_secret|api_key|x-api-key)"?\s*[:=]\s*"?)[^"\s,&}]+'),
        r"\1" + REDACTED,
    )
    _URL_QUERY = (
        re.compile(
            r"(?i)([?&](?:token|access_token|id_token|auth|auth_token|authtoken|sig|signature|key|apikey|api_key|"
            r"sas|password|x-amz-[a-z-]+|x-goog-[a-z-]+)=)[^&#\s\"'<>]+"
        ),
        r"\1" + REDACTED,
    )
    _PATTERNS = (
        (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer " + REDACTED),
        _JWT,
        _KV,
        _URL_QUERY,
    )
    _DATA_PATTERNS = (
        (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"), "Bearer " + REDACTED),
        _JWT,
        _KV,
        _URL_QUERY,
    )

    def __init__(self, values: Iterable[str] = ()) -> None:
        self._values: List[str] = []
        for v in values:
            self.add(v)

    def add(self, value: Optional[str]) -> None:
        if value and len(value) >= 4 and value not in self._values:
            self._values.append(value)
            self._values.sort(key=len, reverse=True)

    def discard(self, value: Optional[str]) -> None:
        if value in self._values:
            self._values.remove(value)

    def _apply(self, s: str, patterns: Any) -> str:
        for v in self._values:
            if v in s:
                s = s.replace(v, REDACTED)
        for pat, repl in patterns:
            s = pat.sub(repl, s)
        return s

    def __call__(self, text: Any) -> str:
        return self._apply(text if isinstance(text, str) else str(text), self._PATTERNS)

    def scrub(self, data: Any) -> Any:
        """Walk dicts and lists and mask secrets in every string value (for response data)."""
        if isinstance(data, str):
            return self._apply(data, self._DATA_PATTERNS)
        if isinstance(data, dict):
            return {k: self.scrub(v) for k, v in data.items()}
        if isinstance(data, (list, tuple)):
            return [self.scrub(v) for v in data]
        return data


def strip_auth_ticket(body: Dict[str, Any]) -> Dict[str, Any]:
    """Drop any authTicket key (case-insensitive). A caller-supplied ticket is never sent."""
    return {k: v for k, v in body.items() if str(k).lower() != "authticket"}


def clamp_count(body: Dict[str, Any], maximum: int = MAX_COUNT) -> Dict[str, Any]:
    """Clamp a `count` value to 1..maximum. Raises SnapStreamError when it is not an integer."""
    if "count" in body and body["count"] is not None:
        try:
            n = int(body["count"])
        except (TypeError, ValueError):
            raise SnapStreamError("Parameter 'count' must be an integer.", kind="invalid_params") from None
        body = {**body, "count": max(1, min(maximum, n))}
    return body


def apply_paging_defaults(body: Dict[str, Any], param_names: Iterable[str], default_count: int = DEFAULT_COUNT) -> Dict[str, Any]:
    """Fill in index=0 and a small count for paged methods when the caller left them out."""
    names = set(param_names)
    out = dict(body)
    if "index" in names and out.get("index") is None:
        out["index"] = 0
    if "count" in names and out.get("count") is None:
        out["count"] = default_count
    return clamp_count(out)


def prepare_body(params: Optional[Dict[str, Any]], *, empty_auth_ticket: bool = False) -> Dict[str, Any]:
    """Strip any caller authTicket, clamp count, and add the {} stub when asked."""
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise SnapStreamError("Request parameters must be a JSON object.", kind="invalid_params")
    body = clamp_count(strip_auth_ticket(params))
    if empty_auth_ticket:
        body = {"authTicket": {}, **body}
    return body


# ------------------------------------------------------------------------ faults
_STACK_LINE = re.compile(r"^\s*(at\s|---\s*End of|Server stack trace|Exception rethrown|\[Exception)", re.I)
_INLINE_STACK = re.compile(r"\s+at\s+[\w.<>`]+\.[\w<>`]+\s*\(.*$", re.S)
_TAG = re.compile(r"<[^>]+>")


def clean_fault_message(text: Any, limit: int = FAULT_MESSAGE_LIMIT) -> str:
    """First meaningful line of a server fault, stack frames removed, about `limit` characters."""
    s = html.unescape(str(text or ""))
    lines = [ln.strip() for ln in s.replace("\r", "\n").split("\n")]
    keep = [ln for ln in lines if ln and not _STACK_LINE.match(ln)]
    first = keep[0] if keep else ""
    first = _INLINE_STACK.sub("", first)
    first = " ".join(first.split())
    return first[:limit] + ("..." if len(first) > limit else "")


def decode_custom_error(value: str) -> str:
    """CustomError is the exception message, UTF-8 then base64. Undecodable values pass as is:
    a value that is not valid base64, is not valid UTF-8 once decoded, or decodes to control
    characters (plain text such as "AccessDenied" happens to be valid base64) is returned raw."""
    raw = (value or "").strip()
    try:
        text = base64.b64decode(raw, validate=True).decode("utf-8")
    except (binascii.Error, ValueError):  # UnicodeDecodeError is a ValueError
        return raw
    if any(unicodedata.category(ch) == "Cc" and ch not in "\t\r\n" for ch in text):
        return raw
    return text


def _xml_text(pattern: str, text: str) -> str:
    m = re.search(pattern, text, re.S | re.I)
    return _TAG.sub(" ", m.group(1)).strip() if m else ""


def body_fault(resp: httpx.Response, redact: Optional[Callable[[str], str]] = None) -> Optional[str]:
    """The fault message when a 2xx body is XML or HTML, or carries a SOAP/WCF fault; else None.
    redact, when given, masks secrets before the message is cut to length, so a secret that
    straddles the cut cannot survive as a prefix."""
    r = redact or (lambda x: x)
    text = resp.text or ""
    stripped = text.lstrip()
    ctype = resp.headers.get("content-type", "").lower()
    markup = "xml" in ctype or "html" in ctype or stripped.startswith("<")
    if not markup:
        try:
            data = json.loads(text)
        except ValueError:
            # Not JSON: only a SOAP/WCF fault marker makes it a fault; other text passes through.
            if not re.search(r"<(?:\w+:)?Fault\b|faultstring", text, re.I):
                return None
        else:
            # Valid JSON is a fault only in WCF's own fault shape, never because of its content.
            if isinstance(data, dict) and ("ExceptionDetail" in data or ("ExceptionType" in data and "Message" in data)):
                detail = data.get("ExceptionDetail") if isinstance(data.get("ExceptionDetail"), dict) else {}
                etype = data.get("ExceptionType") or detail.get("Type") or ""
                msg = clean_fault_message(r(str(data.get("Message") or detail.get("Message") or "")))
                return f"{etype.rsplit('.', 1)[-1]}: {msg}" if etype else msg
            return None
    text = r(text)
    msg = (
        _xml_text(r"<faultstring[^>]*>(.*?)</faultstring>", text)
        or _xml_text(r"<(?:\w+:)?Text[^>]*>(.*?)</(?:\w+:)?Text>", text)
        or _xml_text(r"<(?:\w+:)?Message[^>]*>(.*?)</(?:\w+:)?Message>", text)
        or _xml_text(r"<title[^>]*>(.*?)</title>", text)
        or _xml_text(r"<h1[^>]*>(.*?)</h1>", text)
        or _TAG.sub(" ", text)
    )
    etype = _xml_text(r"<(?:\w+:)?Type[^>]*>(.*?)</(?:\w+:)?Type>", text)
    msg = clean_fault_message(msg) or "unparsable XML or HTML body"
    if etype and etype.rsplit(".", 1)[-1] not in msg:
        return f"{etype.rsplit('.', 1)[-1]}: {msg}"
    return msg


_PRE_EXECUTION_FAULT = re.compile(r"CheckParameterPermissions|GatewayTicketPermissionInspector")


def _fault_hint(message: str) -> str:
    if re.search(r"NullReferenceException|CheckParameterPermissions|GatewayTicketPermissionInspector", message):
        return (
            ' Hint: on builds before the gateway fix this method needs the "authTicket": {} stub '
            "(SNAPSTREAM_MCP_EMPTY_AUTH_TICKET=on and an empty_object auth_ticket in the catalog)."
        )
    return ""


# ---------------------------------------------------------------- pacing, budget
class TokenBucket:
    """Classic token bucket: `rate` tokens per second up to `burst`. acquire() waits for a token."""

    def __init__(self, rate: float, burst: int, clock: Callable[[], float], sleep: Callable[[float], Awaitable[None]]) -> None:
        self.rate = max(0.001, float(rate))
        self.capacity = float(max(1, int(burst)))
        self.tokens = self.capacity
        self._clock = clock
        self._sleep = sleep
        self._updated: Optional[float] = None
        self._lock = asyncio.Lock()
        self.waits = 0

    def _refill(self) -> None:
        now = self._clock()
        if self._updated is None:
            self._updated = now
        self.tokens = min(self.capacity, self.tokens + max(0.0, now - self._updated) * self.rate)
        self._updated = now

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                self._refill()
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                self.waits += 1
                await self._sleep((1.0 - self.tokens) / self.rate)


def _utc_day() -> str:
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


class CallCounter:
    """API calls per UTC day, persisted as a small JSON file so restarts (and other server
    processes) share it. The budget is soft: a file that cannot be read or written never blocks
    a call, it only loses the count."""

    def __init__(self, path: Optional[Path], budget: int, today: Callable[[], str] = _utc_day) -> None:
        self.path = Path(path) if path else None
        self.budget = max(1, int(budget))
        self._today = today
        self.day = ""
        self.calls = 0

    def _sync(self) -> None:
        day = self._today()
        calls = self.calls if self.day == day else 0
        if self.path is not None:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("day") == day and isinstance(data.get("calls"), int):
                    calls = max(calls, data["calls"])
            except (OSError, ValueError):
                pass
        self.day, self.calls = day, calls

    def check(self) -> None:
        self._sync()
        if self.calls >= self.budget:
            raise SnapStreamError(
                f"Daily API call budget reached ({self.calls} of {self.budget} calls on {self.day} UTC). "
                "The whole team shares one API key; raise SNAPSTREAM_MCP_DAILY_BUDGET only if that is intended.",
                kind="budget",
            )

    def record(self) -> None:
        self._sync()
        self.calls += 1
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps({"day": self.day, "calls": self.calls}), encoding="utf-8")
            except OSError:
                pass

    def snapshot(self) -> Dict[str, Any]:
        self._sync()
        return {"day_utc": self.day, "calls_today": self.calls, "budget": self.budget}


# ------------------------------------------------------------------------ client
class SnapStreamClient:
    """One instance per server process. All public methods are coroutines."""

    def __init__(
        self,
        config: Config,
        settings: Optional[Settings] = None,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        catalog: Any = None,
        today: Callable[[], str] = _utc_day,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.config = config
        self.settings = settings or Settings()
        self.redact = Redactor(config.secret_values())
        self.catalog = catalog
        self._transport = transport
        self._clock = clock
        self._sleep = sleep
        self._jitter = jitter
        self._http: Optional[httpx.AsyncClient] = None
        self._token: Optional[str] = None
        self._token_expiry = 0.0
        self._token_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(self.settings.max_concurrency)
        self._last_write_end: Optional[float] = None
        self.bucket = TokenBucket(self.settings.rate_per_sec, self.settings.burst, clock, sleep)
        self.counter = CallCounter(self.settings.call_counter_file, self.settings.daily_budget, today)
        self.token_fetches = 0
        self.retries = 0

    # ----------------------------------------------------------------- plumbing
    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            t = self.settings.timeout_seconds
            self._http = httpx.AsyncClient(
                transport=self._transport,
                timeout=httpx.Timeout(t, connect=min(10.0, t)),
                follow_redirects=False,
                headers={"User-Agent": "snapstream-mcp/0.2"},
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def _excerpt(self, text: str, limit: int = 300) -> str:
        s = " ".join((text or "").split())
        s = self.redact(s)
        return s[:limit] + ("..." if len(s) > limit else "")

    def _error_excerpt(self, resp: httpx.Response, limit: int = 300) -> str:
        """Excerpt of an error body: the fault message for XML or HTML pages, stack frames stripped."""
        fault = body_fault(resp, self.redact)
        if fault:
            return self._excerpt(fault, limit)
        text = self.redact(resp.text or "")
        return self._excerpt(clean_fault_message(text, limit * 2) if "\n" in text else text, limit)

    def usage(self) -> Dict[str, Any]:
        snap = self.counter.snapshot()
        snap.update({"rate_per_sec": self.bucket.rate, "burst": int(self.bucket.capacity), "read_retries": self.retries})
        return snap

    # -------------------------------------------------------------------- token
    def _token_valid(self) -> bool:
        return bool(self._token) and self._clock() < self._token_expiry - TOKEN_REFRESH_MARGIN

    def invalidate_token(self) -> None:
        self.redact.discard(self._token)
        self._token = None
        self._token_expiry = 0.0

    async def get_token(self, force: bool = False) -> str:
        if not force and self._token_valid():
            return self._token  # type: ignore[return-value]
        async with self._token_lock:
            if not force and self._token_valid():
                return self._token  # type: ignore[return-value]
            await self._fetch_token()
            return self._token  # type: ignore[return-value]

    async def _fetch_token(self) -> None:
        cfg = self.config
        form = {"grant_type": "client_credentials", "client_id": cfg.client_id, "client_secret": cfg.client_secret}
        if cfg.scopes:
            form["scope"] = cfg.scopes
        try:
            resp = await self._client().post(cfg.token_endpoint, data=form, headers={"Accept": "application/json"})
        except httpx.TimeoutException:
            raise SnapStreamError("Token request timed out.", kind="auth") from None
        except httpx.HTTPError as exc:
            raise SnapStreamError(f"Token request failed: {type(exc).__name__}: {self.redact(exc)}", kind="auth") from None
        if resp.status_code != 200:
            raise SnapStreamError(
                f"Token request failed: HTTP {resp.status_code}. Check client_id, client_secret, scopes and token_endpoint in secrets.yaml.",
                kind="auth",
                status=resp.status_code,
                excerpt=self._excerpt(resp.text, 200),
            )
        try:
            payload = resp.json()
        except ValueError:
            raise SnapStreamError("Token endpoint returned a non-JSON body.", kind="auth", status=200) from None
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise SnapStreamError("Token endpoint response had no access_token.", kind="auth", status=200)
        try:
            ttl = float(payload.get("expires_in", 3600))
        except (TypeError, ValueError):
            ttl = 3600.0
        self.redact.discard(self._token)
        self._token = token
        self._token_expiry = self._clock() + max(ttl, 1.0)
        self.redact.add(token)
        self.token_fetches += 1

    def token_seconds_left(self) -> int:
        if not self._token:
            return 0
        return int(max(0.0, self._token_expiry - self._clock()))

    # --------------------------------------------------------------------- calls
    async def _post(self, url: str, body: Dict[str, Any], service: str, method: str, *, force_token: bool = False) -> httpx.Response:
        token = await self.get_token(force=force_token)
        headers = {
            "Authorization": f"Bearer {token}",
            "x-api-key": self.config.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self.counter.check()
        await self.bucket.acquire()
        self.counter.record()
        try:
            return await self._client().post(url, json=body, headers=headers)
        except (httpx.ConnectTimeout, httpx.PoolTimeout):
            raise SnapStreamError(
                f"{service}/{method} timed out before the request was sent (connect).",
                kind="timeout", service=service, method=method, sent=False,
            ) from None
        except httpx.TimeoutException:
            raise SnapStreamError(
                f"{service}/{method} timed out after {self.settings.timeout_seconds:g} s; the server may still have run it.",
                kind="timeout", service=service, method=method, sent=True,
            ) from None
        except httpx.ConnectError as exc:
            raise SnapStreamError(
                f"{service}/{method} network error before sending: {type(exc).__name__}: {self.redact(exc)}",
                kind="network", service=service, method=method, sent=False,
            ) from None
        except httpx.HTTPError as exc:
            raise SnapStreamError(
                f"{service}/{method} network error: {type(exc).__name__}: {self.redact(exc)}",
                kind="network", service=service, method=method, sent=True,
            ) from None

    async def _send(self, url: str, service: str, method: str, body: Dict[str, Any]) -> httpx.Response:
        async with self._sem:
            resp = await self._post(url, body, service, method)
            if resp.status_code == 401:
                # The gateway rejected the token before the backend saw the call, so one retry is safe.
                self.invalidate_token()
                resp = await self._post(url, body, service, method, force_token=True)
        return resp

    def _retry_delay(self, resp: httpx.Response, attempt: int) -> float:
        ra = resp.headers.get("Retry-After", "").strip()
        if ra:
            try:
                return max(0.0, min(MAX_RETRY_AFTER, float(ra)))
            except ValueError:
                pass
        return min(8.0, 0.5 * (2 ** (attempt - 1))) + 0.25 * self._jitter()

    def route(self, service: str, method: str) -> tuple:
        """(display service, url path segment, catalog auth_ticket) for a call."""
        info = self.catalog.get(service, method) if self.catalog is not None else None
        if info is not None:
            return info.service, info.path, info.auth_ticket
        return service, SERVICE_URL_PATHS.get(service, service), "omit"

    async def call(
        self,
        service: str,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        write: bool = False,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """POST one API method and return the parsed JSON (None for an empty 200). meta, when
        given, receives request_id and attempts."""
        for label, name in (("service", service), ("method", method)):
            if not isinstance(name, str) or not _NAME_RE.match(name):
                raise SnapStreamError(f"Invalid {label} name.", kind="invalid_params")
        service, path, auth = self.route(service, method)
        if not _NAME_RE.match(path):
            raise SnapStreamError("Invalid service path.", kind="invalid_params")
        body = prepare_body(params, empty_auth_ticket=self.settings.empty_auth_ticket and auth == "empty_object")
        url = f"{self.config.base_url}/{path}/json/{method}"
        if meta is None:
            meta = {}
        if write:
            async with self._write_lock:
                if self._last_write_end is not None:
                    wait = self._last_write_end + self.settings.write_min_interval - self._clock()
                    if wait > 0:
                        await self._sleep(wait)
                try:
                    resp = await self._send(url, service, method, body)  # never retried
                finally:
                    self._last_write_end = self._clock()
            meta["attempts"] = 1
        else:
            attempts = max(1, self.settings.read_attempts)
            for attempt in range(1, attempts + 1):
                resp = await self._send(url, service, method, body)
                meta["attempts"] = attempt
                if resp.status_code in RETRY_READ_STATUSES and attempt < attempts:
                    self.retries += 1
                    await self._sleep(self._retry_delay(resp, attempt))
                    continue
                break
        meta["request_id"] = resp.headers.get(REQUEST_ID_HEADER)
        return self._parse(resp, service, method)

    def _parse(self, resp: httpx.Response, service: str, method: str) -> Any:
        status = resp.status_code
        rid = resp.headers.get(REQUEST_ID_HEADER)
        custom = resp.headers.get(CUSTOM_ERROR_HEADER)
        # A CustomError header that is present at all marks a fault, even when it is empty.
        if custom is not None or (200 <= status < 300 and resp.content and resp.content.strip()):
            # Redact before cutting to length, so a secret that straddles the cut is still masked.
            full = self.redact(decode_custom_error(custom) if custom is not None else (resp.text or ""))
            fault = clean_fault_message(full) if custom is not None else body_fault(resp, self.redact)
            if custom is not None and not fault:
                fault = "the server reported an error with no message"
            if fault:
                msg = self.redact(fault).rstrip(".")
                raise SnapStreamError(
                    f"{service}/{method} failed: the API returned a fault with HTTP {status}: {msg}.{_fault_hint(full)}",
                    kind="api_fault",
                    status=status,
                    service=service,
                    method=method,
                    request_id=rid,
                    sent=True,
                    before_execution=bool(_PRE_EXECUTION_FAULT.search(full)),
                )
        if 200 <= status < 300:
            if not resp.content or not resp.content.strip():
                return None
            try:
                return resp.json()
            except ValueError:
                return {"_non_json_body": self._excerpt(resp.text, 2000)}
        hint = {
            400: "Bad request: check parameter names and types with ss_api_describe.",
            401: "Unauthorized even after a token refresh: check the client credentials.",
            403: "Forbidden: missing or wrong x-api-key (API Gateway). Permission problems come back as faults instead.",
            404: "Not found: check the service and method names.",
            429: "Rate limited by the API gateway: slow down and retry later.",
            502: "Bad gateway: the outcome is unknown.",
            503: "Service unavailable: the outcome is unknown.",
            504: "Gateway timeout (29 s limit): the outcome is unknown.",
        }.get(status, "Server error." if status >= 500 else "Unexpected response.")
        raise SnapStreamError(
            f"{service}/{method} failed: HTTP {status}. {hint}",
            kind="http",
            status=status,
            service=service,
            method=method,
            excerpt=self._error_excerpt(resp),
            request_id=rid,
            sent=True,
        )
