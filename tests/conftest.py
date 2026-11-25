"""Shared offline fixtures. Nothing here touches the network: every HTTP call goes to an
httpx.MockTransport, and the credentials are fake test values."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from snapstream_mcp import server  # noqa: E402
from snapstream_mcp.catalog import Catalog  # noqa: E402
from snapstream_mcp.config import Config, Settings  # noqa: E402

FAKE_CLIENT_ID = "fake-client-id-7f3a9c"
FAKE_CLIENT_SECRET = "fake-client-secret-Q2w9xZ81"
FAKE_API_KEY = "fake-api-key-L0p7Kd52"
BASE_URL = "https://api.example.test/v1"
TOKEN_URL = "https://auth.example.test/oauth2/token"

ITEM_ID = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
CHANNEL_ID = "11111111-2222-3333-4444-555555555555"
FOLDER_PATH_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
FOLDER_ID = "99999999-8888-7777-6666-555555555555"
# A media item the default GetMediaItem route returns (an hour-long recording).
MEDIA_ITEM = {
    "Id": ITEM_ID,
    "Title": "Evening News",
    "ChannelId": CHANNEL_ID,
    "ContextId": "00000000-0000-0000-0000-000000000000",
    "FolderPathId": FOLDER_PATH_ID,
    "FolderId": FOLDER_ID,
    "Filename": "evening-news.mpg",
    "Duration": "PT1H",
}


def run(coro):
    return asyncio.run(coro)


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: List[float] = []

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class MockApi:
    """Records every request. Token endpoint returns tok-1, tok-2, ...; API routes are keyed by
    (service, method) and map to a JSON value, an httpx.Response, or a callable(request)."""

    def __init__(self) -> None:
        self.requests: List[httpx.Request] = []
        self.token_count = 0
        self.expires_in = 3600
        self.token_status = 200
        self.token_body: Optional[str] = None
        # GetMediaItem answers by default: the transcript and smart-chapter permission guard calls it.
        self.routes: Dict[Tuple[str, str], Any] = {("Library", "GetMediaItem"): {"GetMediaItemResult": dict(MEDIA_ITEM)}}
        self.fail_next_401 = 0

    @property
    def api_requests(self) -> List[httpx.Request]:
        return [r for r in self.requests if r.url.host == "api.example.test"]

    def paths(self) -> List[str]:
        """'Service/Method' (the URL path segments) of every API request, in order."""
        out = []
        for r in self.api_requests:
            parts = r.url.path.strip("/").split("/")
            out.append(f"{parts[-3]}/{parts[-1]}")
        return out

    def bodies(self) -> List[Dict[str, Any]]:
        return [json.loads(r.content or b"{}") for r in self.api_requests]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "auth.example.test":
            if self.token_status != 200:
                return httpx.Response(self.token_status, text=self.token_body or '{"error":"invalid_client"}')
            self.token_count += 1
            return httpx.Response(
                200, json={"access_token": f"tok-{self.token_count}-abcdefgh", "expires_in": self.expires_in, "token_type": "Bearer"}
            )
        parts = request.url.path.strip("/").split("/")
        service, method = parts[-3], parts[-1]
        if self.fail_next_401 > 0:
            self.fail_next_401 -= 1
            return httpx.Response(401, json={"message": "Unauthorized"})
        route = self.routes.get((service, method))
        if route is None:
            return httpx.Response(200, json={f"{method}Result": []})
        if callable(route):
            return route(request)
        if isinstance(route, httpx.Response):
            return route
        return httpx.Response(200, json=route)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture(autouse=True)
def _offline_guard(monkeypatch, tmp_path):
    """Never let a test read the real secrets.yaml (and so never reach the real API), and never let
    one write the call counter or audit log into the repo."""
    monkeypatch.setenv("SNAPSTREAM_MCP_SECRETS", str(tmp_path / "no-such-secrets.yaml"))
    monkeypatch.setenv("SNAPSTREAM_MCP_AUDIT_FILE", str(tmp_path / "guard.audit.jsonl"))
    repo_counter = Settings().call_counter_file
    existed = repo_counter.exists()
    server.set_runtime(None)
    yield
    server.set_runtime(None)
    assert existed or not repo_counter.exists(), "a test wrote the daily call counter into the repo"


@pytest.fixture
def cfg() -> Config:
    return Config(
        base_url=BASE_URL,
        token_endpoint=TOKEN_URL,
        client_id=FAKE_CLIENT_ID,
        client_secret=FAKE_CLIENT_SECRET,
        api_key=FAKE_API_KEY,
        instance="test.instance",
        scopes="",
        source_path="test",
    )


@pytest.fixture
def api() -> MockApi:
    return MockApi()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(audit_file=tmp_path / "test.audit.jsonl", write_min_interval=5.0)


@pytest.fixture(scope="session")
def catalog() -> Catalog:
    return Catalog.load()


@pytest.fixture
def rt(cfg, settings, api, catalog, clock):
    """A server Runtime wired to the mock transport and a fake clock."""
    runtime = server.Runtime(
        config=cfg,
        settings=settings,
        catalog=catalog,
        transport=api.transport(),
        client_kwargs={"clock": clock, "sleep": clock.sleep},
    )
    server.set_runtime(runtime)
    yield runtime
    server.set_runtime(None)


def no_secrets(text: str, extra: Tuple[str, ...] = ()) -> bool:
    return not any(s in text for s in (FAKE_CLIENT_ID, FAKE_CLIENT_SECRET, FAKE_API_KEY) + tuple(extra))


def dump(obj: Any) -> str:
    return json.dumps(obj, default=str)
