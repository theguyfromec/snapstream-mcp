"""Async HTTP client for the SnapStream tenant API.

* Access tokens come from the Cognito token endpoint (client_credentials). The token is cached
  and refreshed 60 seconds before it expires; a 401 from the API triggers one refresh and retry.
* Every call is `POST {base_url}/{Service}/json/{Method}` with a JSON body keyed by parameter
  name. Any `authTicket` key is removed (the gateway resolves the ticket itself).
* `count` is clamped to 1..1000. Writes are serialized and spaced by a minimum interval, and a
  semaphore caps concurrent requests.
* Errors are raised as SnapStreamError with the HTTP status and a short body excerpt. Every
  error string passes through the Redactor, which removes credentials and tokens.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional

import httpx

from .config import Config, Settings

MAX_COUNT = 1000
DEFAULT_COUNT = 50
TOKEN_REFRESH_MARGIN = 60.0
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,99}$")
REDACTED = "[REDACTED]"


class SnapStreamError(Exception):
    """An API, auth or transport failure. The message is already redacted."""

    def __init__(
        self,
        message: str,
        *,
        kind: str = "api",
        status: Optional[int] = None,
        service: Optional[str] = None,
        method: Optional[str] = None,
        excerpt: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.status = status
        self.service = service
        self.method = method
        self.excerpt = excerpt

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"error": self.message, "kind": self.kind}
        if self.status is not None:
            out["http_status"] = self.status
        if self.service and self.method:
            out["method"] = f"{self.service}/{self.method}"
        if self.excerpt:
            out["body_excerpt"] = self.excerpt
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
    """Drop any authTicket key (case-insensitive). The gateway resolves the ticket itself."""
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


def prepare_body(params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise SnapStreamError("Request parameters must be a JSON object.", kind="invalid_params")
    return clamp_count(strip_auth_ticket(params))


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
    ) -> None:
        self.config = config
        self.settings = settings or Settings()
        self.redact = Redactor(config.secret_values())
        self._transport = transport
        self._clock = clock
        self._sleep = sleep
        self._http: Optional[httpx.AsyncClient] = None
        self._token: Optional[str] = None
        self._token_expiry = 0.0
        self._token_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(self.settings.max_concurrency)
        self._last_write_end: Optional[float] = None
        self.token_fetches = 0

    # ----------------------------------------------------------------- plumbing
    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            t = self.settings.timeout_seconds
            self._http = httpx.AsyncClient(
                transport=self._transport,
                timeout=httpx.Timeout(t, connect=min(10.0, t)),
                follow_redirects=False,
                headers={"User-Agent": "snapstream-mcp/0.1"},
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
        try:
            return await self._client().post(url, json=body, headers=headers)
        except httpx.TimeoutException:
            raise SnapStreamError(
                f"{service}/{method} timed out after {self.settings.timeout_seconds:g} s.",
                kind="timeout",
                service=service,
                method=method,
            ) from None
        except httpx.HTTPError as exc:
            raise SnapStreamError(
                f"{service}/{method} network error: {type(exc).__name__}: {self.redact(exc)}",
                kind="network",
                service=service,
                method=method,
            ) from None

    async def _send(self, service: str, method: str, body: Dict[str, Any]) -> httpx.Response:
        url = f"{self.config.base_url}/{service}/json/{method}"
        async with self._sem:
            resp = await self._post(url, body, service, method)
            if resp.status_code == 401:
                self.invalidate_token()
                resp = await self._post(url, body, service, method, force_token=True)
        return resp

    async def call(self, service: str, method: str, params: Optional[Dict[str, Any]] = None, *, write: bool = False) -> Any:
        """POST one API method and return the parsed JSON (None for an empty 200)."""
        for label, name in (("service", service), ("method", method)):
            if not isinstance(name, str) or not _NAME_RE.match(name):
                raise SnapStreamError(f"Invalid {label} name.", kind="invalid_params")
        body = prepare_body(params)
        if write:
            async with self._write_lock:
                if self._last_write_end is not None:
                    wait = self._last_write_end + self.settings.write_min_interval - self._clock()
                    if wait > 0:
                        await self._sleep(wait)
                try:
                    resp = await self._send(service, method, body)
                finally:
                    self._last_write_end = self._clock()
        else:
            resp = await self._send(service, method, body)
        return self._parse(resp, service, method)

    def _parse(self, resp: httpx.Response, service: str, method: str) -> Any:
        status = resp.status_code
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
            403: "Forbidden: the API user may lack a permission listed in ss_api_describe, or the api_key is wrong.",
            404: "Not found: check the service and method names.",
            429: "Rate limited by the API gateway: slow down and retry later.",
        }.get(status, "Server error." if status >= 500 else "Unexpected response.")
        raise SnapStreamError(
            f"{service}/{method} failed: HTTP {status}. {hint}",
            kind="http",
            status=status,
            service=service,
            method=method,
            excerpt=self._excerpt(resp.text),
        )
