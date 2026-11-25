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

_AUDIT = {}


@pytest.fixture(autouse=True)
def _tmp_audit(tmp_path):
    """Keep the daily call counter (written beside the audit file) inside the test's tmp dir."""
    _AUDIT["path"] = tmp_path / "client.audit.jsonl"
    yield


def tmp_settings(**kw):
    kw.setdefault("audit_file", _AUDIT["path"])
    return Settings(**kw)


def make_client(cfg, api, clock, catalog=None, **settings_kw):
    s = tmp_settings(**settings_kw)
    return SnapStreamClient(cfg, s, transport=api.transport(), clock=clock, sleep=clock.sleep, catalog=catalog)


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

    c = SnapStreamClient(cfg, tmp_settings(), transport=httpx.MockTransport(boom), clock=clock, sleep=clock.sleep)
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

    c = SnapStreamClient(cfg, tmp_settings(max_concurrency=2), transport=httpx.MockTransport(handler), clock=clock)

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


# ------------------------------------------------------------ url paths
import base64  # noqa: E402

from snapstream_mcp.client import CallCounter, TokenBucket, clean_fault_message, decode_custom_error  # noqa: E402
from snapstream_mcp.config import DEFAULT_TIMEOUT  # noqa: E402


@pytest.mark.parametrize("service,method,path", [
    ("Configuration", "GetApiLevel", "/v1/Config/json/GetApiLevel"),
    ("Config", "GetApiLevel", "/v1/Config/json/GetApiLevel"),
    ("LocalConfiguration", "GetApiLevel", "/v1/LocalConfig/json/GetApiLevel"),
    ("NotifierManager", "GetMyAlerts", "/v1/Notifier/json/GetMyAlerts"),
    ("Library", "GetMediaItems", "/v1/Library/json/GetMediaItems"),
])
def test_url_built_from_catalog_url_path(cfg, api, clock, catalog, service, method, path):
    c = make_client(cfg, api, clock, catalog=catalog)
    run(c.call(service, method, {}))
    assert api.api_requests[-1].url.path == path


def test_url_path_fallback_without_catalog(cfg, api, clock):
    c = make_client(cfg, api, clock)
    run(c.call("NotifierManager", "GetMyAlerts", {}))
    assert api.api_requests[-1].url.path == "/v1/Notifier/json/GetMyAlerts"


# ----------------------------------------------------------- authTicket
CLIP = {"clipProperties": {"ClipPropertiesType": 1}}


def test_empty_auth_ticket_sent_for_permission_on_input_methods(cfg, api, clock, catalog):
    c = make_client(cfg, api, clock, catalog=catalog)
    run(c.call("TaskManager", "ClipFile", {**CLIP, "authTicket": {"Id": "real-ticket"}}, write=True))
    body = api.bodies()[-1]
    assert body["authTicket"] == {} and body["clipProperties"] == CLIP["clipProperties"]
    assert list(body)[0] == "authTicket"
    run(c.call("Library", "GetMediaItems", {"index": 0, "count": 1, "AuthTicket": {"x": 1}}))
    assert "authTicket" not in api.bodies()[-1] and "AuthTicket" not in api.bodies()[-1]
    run(c.call("TaskManager", "AiClipDesigner", CLIP, write=True))  # not flagged: omitted
    assert "authTicket" not in api.bodies()[-1]


def test_empty_auth_ticket_setting_off(cfg, api, clock, catalog):
    c = make_client(cfg, api, clock, catalog=catalog, empty_auth_ticket=False)
    run(c.call("TaskManager", "ClipFile", {**CLIP, "authTicket": {}}, write=True))
    assert api.bodies()[-1] == CLIP


def test_empty_auth_ticket_env(monkeypatch):
    monkeypatch.setenv("SNAPSTREAM_MCP_EMPTY_AUTH_TICKET", "off")
    assert Settings.from_env().empty_auth_ticket is False
    monkeypatch.setenv("SNAPSTREAM_MCP_EMPTY_AUTH_TICKET", "on")
    assert Settings.from_env().empty_auth_ticket is True
    monkeypatch.delenv("SNAPSTREAM_MCP_EMPTY_AUTH_TICKET")
    assert Settings.from_env().empty_auth_ticket is True


# --------------------------------------------------------------- faults
def _b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


TRACE = (
    "Object reference not set to an instance of an object.\n"
    "   at SnapStream.Gateway.GatewayTicketPermissionInspector.CheckParameterPermissions(Object[] inputs)\n"
    "   at System.ServiceModel.Dispatcher.DispatchOperationRuntime.InvokeBegin(MessageRpc& rpc)"
)


def test_custom_error_header_with_empty_body(cfg, api, clock):
    c = make_client(cfg, api, clock)
    api.routes[("Library", "GetMediaItem")] = httpx.Response(
        200, content=b"", headers={"CustomError": _b64("Access denied to item."), "x-amzn-RequestId": "req-123"}
    )
    with pytest.raises(SnapStreamError) as ei:
        run(c.call("Library", "GetMediaItem", {"itemId": "x"}))
    e = ei.value
    assert e.kind == "api_fault" and e.status == 200 and "Access denied to item." in e.message
    assert e.request_id == "req-123" and e.to_dict()["request_id"] == "req-123"


def test_custom_error_header_with_body_and_trace(cfg, api, clock):
    c = make_client(cfg, api, clock)
    api.routes[("Library", "GetMediaItem")] = httpx.Response(
        200, json={"GetMediaItemResult": None}, headers={"CustomError": _b64(TRACE)}
    )
    with pytest.raises(SnapStreamError) as ei:
        run(c.call("Library", "GetMediaItem", {"itemId": "x"}))
    msg = ei.value.message
    assert ei.value.kind == "api_fault"
    assert "Object reference not set" in msg and "DispatchOperationRuntime" not in msg and "   at " not in msg
    assert "authTicket" in msg  # NullReference hint


def test_custom_error_jwt_is_redacted(cfg, api, clock):
    jwt = "eyJhbGciOiJSUzI1NiJ9.eyJjbGllbnRfaWQiOiJ4eXoifQ.c2lnbmF0dXJlLXZhbHVl"
    c = make_client(cfg, api, clock)
    api.routes[("Library", "GetMediaItem")] = httpx.Response(
        200, content=b"", headers={"CustomError": _b64(f"Permission denied for ticket {jwt} key {FAKE_API_KEY}")}
    )
    with pytest.raises(SnapStreamError) as ei:
        run(c.call("Library", "GetMediaItem", {"itemId": "x"}))
    text = json.dumps(ei.value.to_dict())
    assert jwt not in text and "eyJhbGci" not in text and no_secrets(text)
    assert REDACTED in text


SOAP_FAULT = """<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><s:Fault>
<faultcode>s:Client</faultcode><faultstring xml:lang="en-US">Object reference not set to an instance of an object.
   at SnapStream.Services.TaskManager.ClipFile(ClipProperties clipProperties)
   at SyncInvokeClipFile(Object , Object[] , Object[] )</faultstring>
<detail><ExceptionDetail><Type>System.NullReferenceException</Type></ExceptionDetail></detail></s:Fault></s:Body></s:Envelope>"""


def test_200_xml_fault_raises(cfg, api, clock):
    c = make_client(cfg, api, clock)
    api.routes[("TaskManager", "ClipFile")] = httpx.Response(200, text=SOAP_FAULT, headers={"content-type": "application/xml"})
    with pytest.raises(SnapStreamError) as ei:
        run(c.call("TaskManager", "ClipFile", CLIP, write=True))
    e = ei.value
    assert e.kind == "api_fault" and e.status == 200
    assert "NullReferenceException: Object reference not set" in e.message
    assert "SyncInvokeClipFile" not in e.message and "TaskManager.ClipFile(" not in e.message


def test_200_html_and_wcf_json_faults_raise(cfg, api, clock):
    c = make_client(cfg, api, clock)
    api.routes[("Library", "GetMediaItem")] = httpx.Response(
        200, text="<html><head><title>Request Error</title></head><body>The server encountered an error</body></html>",
        headers={"content-type": "text/html"},
    )
    with pytest.raises(SnapStreamError, match="Request Error") as ei:
        run(c.call("Library", "GetMediaItem", {}))
    assert ei.value.kind == "api_fault"
    api.routes[("Library", "GetMediaItem")] = httpx.Response(
        200, json={"ExceptionDetail": {"Message": "boom"}, "ExceptionType": "System.ArgumentException", "Message": "Bad column\n   at X.Y()"}
    )
    with pytest.raises(SnapStreamError, match="ArgumentException: Bad column") as ei:
        run(c.call("Library", "GetMediaItem", {}))


def test_400_html_page_excerpt_has_no_trace(cfg, api, clock):
    c = make_client(cfg, api, clock)
    page = "<html><head><title>Bad Request</title></head><body><h1>Bad Request</h1><pre>" + TRACE + "</pre></body></html>"
    api.routes[("Library", "GetMediaItem")] = httpx.Response(400, text=page, headers={"content-type": "text/html", "x-amzn-RequestId": "r-400"})
    with pytest.raises(SnapStreamError) as ei:
        run(c.call("Library", "GetMediaItem", {}))
    e = ei.value
    assert e.kind == "http" and e.status == 400 and e.request_id == "r-400"
    assert "Bad Request" in e.excerpt and "Dispatcher" not in e.excerpt and "<" not in e.excerpt


def test_clean_fault_message_and_decode():
    assert clean_fault_message(TRACE) == "Object reference not set to an instance of an object."
    assert len(clean_fault_message("x" * 500)) <= 203
    assert clean_fault_message("Boom at SnapStream.Services.Foo.Bar(Int32 x) at Y.Z()") == "Boom"
    assert decode_custom_error(_b64("héllo")) == "héllo"
    assert decode_custom_error("not base64 !!") == "not base64 !!"


def test_request_id_in_meta(cfg, api, clock):
    c = make_client(cfg, api, clock)
    api.routes[("Library", "GetMediaItemCount")] = httpx.Response(200, json={"GetMediaItemCountResult": 3}, headers={"x-amzn-RequestId": "ok-1"})
    meta = {}
    assert run(c.call("Library", "GetMediaItemCount", {}, meta=meta)) == {"GetMediaItemCountResult": 3}
    assert meta == {"request_id": "ok-1", "attempts": 1}


# ------------------------------------------------------- timeouts, network
def test_default_timeout_is_under_gateway_limit(monkeypatch):
    assert DEFAULT_TIMEOUT == 28.0 and Settings().timeout_seconds == 28.0
    monkeypatch.delenv("SNAPSTREAM_MCP_TIMEOUT", raising=False)
    assert Settings.from_env().timeout_seconds == 28.0


@pytest.mark.parametrize("exc,kind,sent", [
    (httpx.ReadTimeout("slow"), "timeout", True),
    (httpx.ConnectTimeout("slow connect"), "timeout", False),
    (httpx.ConnectError("refused"), "network", False),
    (httpx.RemoteProtocolError("dropped"), "network", True),
])
def test_transport_failures_record_whether_sent(cfg, clock, exc, kind, sent):
    def handler(request):
        if request.url.host == "auth.example.test":
            return httpx.Response(200, json={"access_token": "tok-z-abcdefgh", "expires_in": 3600})
        raise exc

    c = SnapStreamClient(cfg, tmp_settings(), transport=httpx.MockTransport(handler), clock=clock, sleep=clock.sleep)
    with pytest.raises(SnapStreamError) as ei:
        run(c.call("Library", "GetMediaItemCount", {}))
    assert ei.value.kind == kind and ei.value.sent is sent


# ------------------------------------------------------------ rate limits
def test_token_bucket_paces_calls(cfg, api, clock):
    c = make_client(cfg, api, clock, rate_per_sec=2.0, burst=2)
    start = clock.now

    async def go():
        for _ in range(6):
            await c.call("Library", "GetMediaItemCount", {})

    run(go())
    assert len(api.api_requests) == 6
    # Two go out at once (burst); the other four wait half a second each.
    assert clock.now - start == pytest.approx(2.0)
    assert c.bucket.waits == 4


def test_token_bucket_refills_over_time(clock):
    b = TokenBucket(rate=1.0, burst=3, clock=clock, sleep=clock.sleep)

    async def take(n):
        for _ in range(n):
            await b.acquire()

    run(take(3))
    assert clock.sleeps == []
    clock.advance(2.0)
    run(take(2))
    assert clock.sleeps == []
    run(take(1))
    assert clock.sleeps == [pytest.approx(1.0)]


def test_read_retried_on_429_with_retry_after(cfg, api, clock):
    c = make_client(cfg, api, clock)
    seq = [httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(503), httpx.Response(200, json={"GetMediaItemCountResult": 5})]
    api.routes[("Library", "GetMediaItemCount")] = lambda req: seq.pop(0)
    meta = {}
    assert run(c.call("Library", "GetMediaItemCount", {}, meta=meta)) == {"GetMediaItemCountResult": 5}
    assert meta["attempts"] == 3 and len(api.api_requests) == 3
    assert clock.sleeps[0] == pytest.approx(2.0)
    assert 1.0 <= clock.sleeps[1] <= 1.25  # jittered backoff for the 503 without Retry-After


def test_read_retries_stop_after_three_attempts(cfg, api, clock):
    c = make_client(cfg, api, clock)
    api.routes[("Library", "GetMediaItemCount")] = httpx.Response(429)
    with pytest.raises(SnapStreamError) as ei:
        run(c.call("Library", "GetMediaItemCount", {}))
    assert ei.value.status == 429 and len(api.api_requests) == 3


@pytest.mark.parametrize("status", [429, 503])
def test_writes_are_never_retried(cfg, api, clock, status):
    c = make_client(cfg, api, clock)
    api.routes[("Library", "SaveBookmark")] = httpx.Response(status, headers={"Retry-After": "1"})
    with pytest.raises(SnapStreamError) as ei:
        run(c.call("Library", "SaveBookmark", {"item": {}}, write=True))
    assert ei.value.status == status and len(api.api_requests) == 1


def test_daily_budget_refuses_and_persists(cfg, api, clock):
    day = {"d": "2026-11-25"}
    c = SnapStreamClient(cfg, tmp_settings(daily_budget=3), transport=api.transport(), clock=clock, sleep=clock.sleep,
                         today=lambda: day["d"])

    async def go(n):
        for _ in range(n):
            await c.call("Library", "GetMediaItemCount", {})

    run(go(3))
    with pytest.raises(SnapStreamError) as ei:
        run(go(1))
    assert ei.value.kind == "budget" and len(api.api_requests) == 3
    counter_file = _AUDIT["path"].with_name("snapstream_calls.daily.json")
    assert json.loads(counter_file.read_text(encoding="utf-8")) == {"day": "2026-11-25", "calls": 3}
    # A second client (a restart) sees the same count.
    c2 = SnapStreamClient(cfg, tmp_settings(daily_budget=3), transport=api.transport(), clock=clock, sleep=clock.sleep,
                          today=lambda: day["d"])
    assert c2.usage()["calls_today"] == 3
    with pytest.raises(SnapStreamError):
        run(c2.call("Library", "GetMediaItemCount", {}))
    day["d"] = "2026-11-26"  # a new UTC day resets the count
    run(go(1))
    assert c.usage()["calls_today"] == 1 and len(api.api_requests) == 4


def test_call_counter_tolerates_bad_file(tmp_path):
    f = tmp_path / "counter.json"
    f.write_text("not json", encoding="utf-8")
    cc = CallCounter(f, 10, today=lambda: "2026-11-25")
    cc.check()
    cc.record()
    assert cc.snapshot() == {"day_utc": "2026-11-25", "calls_today": 1, "budget": 10}


def test_rate_settings_from_env(monkeypatch):
    monkeypatch.setenv("SNAPSTREAM_MCP_RATE_PER_SEC", "4")
    monkeypatch.setenv("SNAPSTREAM_MCP_BURST", "6")
    monkeypatch.setenv("SNAPSTREAM_MCP_DAILY_BUDGET", "500")
    s = Settings.from_env()
    assert (s.rate_per_sec, s.burst, s.daily_budget) == (4.0, 6, 500)
    d = Settings()
    assert (d.rate_per_sec, d.burst, d.daily_budget) == (8.0, 16, 20000)


def test_json_content_mentioning_faults_is_not_a_fault(cfg, api, clock):
    c = make_client(cfg, api, clock)
    api.routes[("Search", "SearchRecordings")] = {"SearchRecordingsResult": {"Results": [{"Text": "a <Fault> and faultstring"}]}}
    out = run(c.call("Search", "SearchRecordings", {}))
    assert out["SearchRecordingsResult"]["Results"][0]["Text"].startswith("a <Fault>")
    api.routes[("Search", "SearchRecordings")] = httpx.Response(200, text="oops faultstring: Boom")
    with pytest.raises(SnapStreamError) as ei:
        run(c.call("Search", "SearchRecordings", {}))
    assert ei.value.kind == "api_fault"


# ---------------------------------------------------- empty CustomError header (R2)
def test_empty_custom_error_header_is_a_fault(cfg, api, clock):
    c = make_client(cfg, api, clock)
    api.routes[("Scheduler", "RecordAiring")] = httpx.Response(
        200, json={"RecordAiringResult": None}, headers={"CustomError": ""}
    )
    with pytest.raises(SnapStreamError) as ei:
        run(c.call("Scheduler", "RecordAiring", {}, write=True))
    assert ei.value.kind == "api_fault" and ei.value.status == 200
    assert "with no message" in ei.value.message


# ------------------------------------------ plain-text CustomError passes through (R3)
def test_decode_custom_error_keeps_plain_text():
    for plain in ("AccessDenied", "Unauthorized", "NotFound", "Forbidden", "Access denied"):
        assert decode_custom_error(plain) == plain
    assert decode_custom_error(_b64("Access is denied.")) == "Access is denied."
    assert decode_custom_error(_b64("line one\r\nline two\tend")) == "line one\r\nline two\tend"
    invalid_utf8 = base64.b64encode(b"\xff\xfe\xfd\xfc").decode()
    assert decode_custom_error(invalid_utf8) == invalid_utf8


# ------------------------------------- secrets straddling the 200-char cut (SEC-1)
def _no_secret_fragment(text):
    for secret in (FAKE_CLIENT_ID, FAKE_CLIENT_SECRET, FAKE_API_KEY):
        for i in range(len(secret) - 5):
            frag = secret[i:i + 6]
            # Fragments shared with ordinary words are not evidence of a leak.
            if frag.isalpha():
                continue
            assert frag not in text, (frag, text)


@pytest.mark.parametrize("pad", [185, 190, 195, 199])
def test_secret_across_fault_cut_is_redacted(cfg, api, clock, pad):
    c = make_client(cfg, api, clock)
    msg = "x" * pad + " " + FAKE_CLIENT_SECRET + " trailing words"
    cases = [
        httpx.Response(200, content=b"", headers={"CustomError": _b64(msg)}),  # CustomError path
        httpx.Response(200, text=f"<html><title>{msg}</title></html>", headers={"content-type": "text/html"}),  # 2xx HTML
        httpx.Response(500, text=f"<html><title>{msg}</title></html>", headers={"content-type": "text/html"}),  # 500 excerpt
        httpx.Response(200, json={"ExceptionType": "System.Exception", "Message": msg}),  # WCF JSON fault
        httpx.Response(500, text=msg + "\nsecond line"),  # plain multi-line error body
    ]
    for resp in cases:
        api.routes[("Library", "SaveBookmark")] = resp
        with pytest.raises(SnapStreamError) as ei:
            run(c.call("Library", "SaveBookmark", {}, write=True))
        _no_secret_fragment(json.dumps(ei.value.to_dict()) + ei.value.message)
