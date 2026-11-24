import asyncio
import json

import httpx
import pytest

from snapstream_mcp.client import (
    REDACTED,
    Redactor,
    SnapStreamClient,
    SnapStreamError,
    apply_paging_defaults,
    clamp_count,
    strip_auth_ticket,
)
from snapstream_mcp.config import Settings

from .conftest import FAKE_API_KEY, FAKE_CLIENT_ID, FAKE_CLIENT_SECRET, no_secrets, run


def make_client(cfg, api, clock, **settings_kw):
    s = Settings(**settings_kw)
    return SnapStreamClient(cfg, s, transport=api.transport(), clock=clock, sleep=clock.sleep)


def test_headers_url_and_body(cfg, api, clock):
    c = make_client(cfg, api, clock)
    api.routes[("Library", "GetMediaItems")] = {"GetMediaItemsResult": [{"Id": "x"}]}
    out = run(c.call("Library", "GetMediaItems", {"index": 0, "count": 2}))
    assert out == {"GetMediaItemsResult": [{"Id": "x"}]}
    req = api.api_requests[-1]
    assert req.method == "POST"
    assert str(req.url) == "https://api.example.test/v1/Library/json/GetMediaItems"
    assert req.headers["authorization"] == "Bearer tok-1-abcdefgh"
    assert req.headers["x-api-key"] == FAKE_API_KEY
    assert req.headers["content-type"] == "application/json"
    assert json.loads(req.content) == {"index": 0, "count": 2}


def test_token_request_form(cfg, api, clock):
    c = make_client(cfg, api, clock)
    run(c.get_token())
    tok_req = [r for r in api.requests if r.url.host == "auth.example.test"][0]
    form = dict(x.split("=", 1) for x in tok_req.content.decode().split("&"))
    assert form["grant_type"] == "client_credentials"
    assert form["client_id"] == FAKE_CLIENT_ID
    assert "scope" not in form  # empty scopes are not sent


def test_token_cached(cfg, api, clock):
    c = make_client(cfg, api, clock)

    async def go():
        for _ in range(3):
            await c.call("Library", "GetMediaItems", {"index": 0, "count": 1})

    run(go())
    assert api.token_count == 1


def test_token_refreshes_60s_before_expiry(cfg, api, clock):
    c = make_client(cfg, api, clock)
    run(c.call("Library", "GetMediaItemCount"))
    assert api.token_count == 1
    clock.advance(3600 - 61)  # 61 s left: still valid
    run(c.call("Library", "GetMediaItemCount"))
    assert api.token_count == 1
    clock.advance(2)  # 59 s left: refresh
    run(c.call("Library", "GetMediaItemCount"))
    assert api.token_count == 2
    assert api.api_requests[-1].headers["authorization"] == "Bearer tok-2-abcdefgh"


def test_401_triggers_one_refresh_and_retry(cfg, api, clock):
    c = make_client(cfg, api, clock)
    run(c.get_token())
    api.fail_next_401 = 1
    api.routes[("Library", "GetMediaItemCount")] = {"GetMediaItemCountResult": 7}
    out = run(c.call("Library", "GetMediaItemCount"))
    assert out == {"GetMediaItemCountResult": 7}
    assert api.token_count == 2
    assert len(api.api_requests) == 2


def test_401_twice_raises(cfg, api, clock):
    c = make_client(cfg, api, clock)
    api.fail_next_401 = 2
    with pytest.raises(SnapStreamError) as ei:
        run(c.call("Library", "GetMediaItemCount"))
    assert ei.value.status == 401
    assert len(api.api_requests) == 2  # exactly one retry


def test_auth_ticket_stripped(cfg, api, clock):
    c = make_client(cfg, api, clock)
    run(c.call("Library", "GetMediaItems", {"authTicket": None, "AuthTicket": {"x": 1}, "index": 0, "count": 1}))
    body = api.bodies()[-1]
    assert body == {"index": 0, "count": 1}
    assert strip_auth_ticket({"authticket": 1, "a": 2}) == {"a": 2}


def test_count_cap(cfg, api, clock):
    c = make_client(cfg, api, clock)
    run(c.call("Library", "GetMediaItems", {"index": 0, "count": 5000}))
    assert api.bodies()[-1]["count"] == 1000
    assert clamp_count({"count": 0})["count"] == 1
    assert clamp_count({"count": "20"})["count"] == 20
    with pytest.raises(SnapStreamError):
        clamp_count({"count": "lots"})


def test_paging_defaults():
    out = apply_paging_defaults({}, ["index", "count", "sorts"])
    assert out == {"index": 0, "count": 50}
    assert apply_paging_defaults({"count": 9999}, ["index", "count"])["count"] == 1000
    assert apply_paging_defaults({}, ["itemId"]) == {}


def test_empty_and_non_json_bodies(cfg, api, clock):
    c = make_client(cfg, api, clock)
    api.routes[("Library", "SaveMediaItem")] = httpx.Response(200, content=b"")
    api.routes[("Library", "GetLastChange")] = httpx.Response(200, text="not json")
    assert run(c.call("Library", "SaveMediaItem", {})) is None
    assert run(c.call("Library", "GetLastChange", {})) == {"_non_json_body": "not json"}


def test_invalid_names_rejected(cfg, api, clock):
    c = make_client(cfg, api, clock)
    with pytest.raises(SnapStreamError):
        run(c.call("Library/../x", "GetMediaItems"))
    with pytest.raises(SnapStreamError):
        run(c.call("Library", "Get MediaItems"))
    assert api.requests == []


def test_http_error_is_redacted(cfg, api, clock):
    c = make_client(cfg, api, clock)

    def echo(request):
        # A hostile or buggy upstream that echoes every credential back.
        return httpx.Response(
            500,
            text=f"boom api_key={FAKE_API_KEY} secret {FAKE_CLIENT_SECRET} id {FAKE_CLIENT_ID} "
            f"auth {request.headers['authorization']}",
        )

    api.routes[("Library", "GetMediaItems")] = echo
    with pytest.raises(SnapStreamError) as ei:
        run(c.call("Library", "GetMediaItems", {"index": 0, "count": 1}))
    err = ei.value
    text = json.dumps(err.to_dict()) + str(err)
    assert err.status == 500
    assert no_secrets(text, extra=("tok-1-abcdefgh",))
    assert REDACTED in text


def test_token_error_is_redacted(cfg, api, clock):
    api.token_status = 400
    api.token_body = f'{{"error":"invalid_client","client_secret":"{FAKE_CLIENT_SECRET}","id":"{FAKE_CLIENT_ID}"}}'
    c = make_client(cfg, api, clock)
    with pytest.raises(SnapStreamError) as ei:
        run(c.get_token())
    text = json.dumps(ei.value.to_dict()) + str(ei.value)
    assert ei.value.kind == "auth" and ei.value.status == 400
    assert no_secrets(text)


def test_network_error_is_redacted(cfg, clock):
    def boom(request):
        raise httpx.ConnectError(f"cannot connect with key {FAKE_API_KEY}")

    c = SnapStreamClient(cfg, Settings(), transport=httpx.MockTransport(boom), clock=clock, sleep=clock.sleep)
    with pytest.raises(SnapStreamError) as ei:
        run(c.get_token())
    assert no_secrets(str(ei.value))


def test_write_throttle(cfg, api, clock):
    c = make_client(cfg, api, clock, write_min_interval=5.0)

    async def go():
        await c.call("Library", "SaveBookmark", {"item": {}}, write=True)
        await c.call("Library", "SaveBookmark", {"item": {}}, write=True)
        await c.call("Library", "GetMediaItemCount")  # reads are not throttled

    run(go())
    assert len(clock.sleeps) == 1
    assert clock.sleeps[0] == pytest.approx(5.0)


def test_concurrency_cap(cfg, clock):
    state = {"live": 0, "peak": 0}

    async def handler(request):
        if request.url.host == "auth.example.test":
            return httpx.Response(200, json={"access_token": "tok-x-abcdefgh", "expires_in": 3600})
        state["live"] += 1
        state["peak"] = max(state["peak"], state["live"])
        await asyncio.sleep(0.01)
        state["live"] -= 1
        return httpx.Response(200, json={"GetMediaItemCountResult": 1})

    c = SnapStreamClient(cfg, Settings(max_concurrency=2), transport=httpx.MockTransport(handler), clock=clock)

    async def go():
        await asyncio.gather(*(c.call("Library", "GetMediaItemCount") for _ in range(8)))

    run(go())
    assert state["peak"] == 2


def test_redactor_patterns():
    r = Redactor(["supersecretvalue"])
    s = r('x supersecretvalue Bearer abc.def.ghi "access_token": "zzz" eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.sig')
    assert "supersecretvalue" not in s and "abc.def.ghi" not in s and "zzz" not in s and "eyJhbGci" not in s


def test_redactor_masks_url_query_credentials():
    r = Redactor()
    s = r("https://host/live?token=abc123&X-Amz-Signature=deadbeef&X-Amz-Credential=AKIAX&sig=zz9&key=kk1&channel=5")
    for v in ("abc123", "deadbeef", "AKIAX", "zz9", "kk1"):
        assert v not in s
    assert "channel=5" in s


def test_redactor_scrub_walks_data_and_keeps_prose():
    r = Redactor([FAKE_API_KEY])
    data = {"a": [f"k={FAKE_API_KEY}", {"b": "the bearer of bad news"}], "n": 5, "t": None,
            "c": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123"}
    out = r.scrub(data)
    assert FAKE_API_KEY not in json.dumps(out)
    assert out["a"][1]["b"] == "the bearer of bad news"
    assert "abcdefghijklmnopqrstuvwxyz0123" not in out["c"]
    assert out["n"] == 5 and out["t"] is None
