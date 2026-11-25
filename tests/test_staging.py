import asyncio
import json

import pytest

from snapstream_mcp.client import SnapStreamClient
from snapstream_mcp.staging import WriteStager, payload_hash

from .conftest import FAKE_API_KEY, FAKE_CLIENT_SECRET, no_secrets, run

GUID = "3fa85f64-5717-4562-b3fc-2c963f66afa6"


@pytest.fixture
def stager(catalog, settings, clock):
    return WriteStager(catalog, settings.audit_file, clock=clock)


@pytest.fixture
def client(cfg, settings, api, clock):
    return SnapStreamClient(cfg, settings, transport=api.transport(), clock=clock, sleep=clock.sleep)


def stage_record(stager):
    return stager.stage(
        "Scheduler", "RecordAiring",
        {"seriesId": GUID, "airingId": GUID.upper(), "waitForRebuild": True, "notes": "test", "authTicket": "drop me"},
        reason="unit test",
    )


def test_stage_returns_token_and_preview(stager):
    r = stage_record(stager)
    assert r["status"] == "staged"
    assert r["token"].startswith("ssw_")
    assert r["expires_in_seconds"] == 600
    assert "Scheduler/RecordAiring" in r["preview"] and r["payload_sha256"] in r["preview"]
    assert stager.pending()["count"] == 1


def test_commit_sends_exactly_the_staged_payload(stager, client, api):
    r = stage_record(stager)
    sw, err = stager.take(r["token"])
    assert err is None
    expected = json.loads(sw.payload_json)
    assert "authTicket" not in expected and expected["airingId"] == GUID  # normalized at stage time
    out = run(stager.execute(sw, client))
    assert out["status"] == "committed"
    assert len(api.api_requests) == 1
    req = api.api_requests[0]
    assert req.url.path.endswith("/Scheduler/json/RecordAiring")
    sent = json.loads(req.content)
    assert sent == expected
    assert payload_hash(sent) == r["payload_sha256"]


def test_token_is_single_use(stager, client, api):
    r = stage_record(stager)
    sw, _ = stager.take(r["token"])
    run(stager.execute(sw, client))
    sw2, err = stager.take(r["token"])
    assert sw2 is None and err["status"] == "refused"
    assert len(api.api_requests) == 1


def test_ttl_expiry(stager, clock, api):
    r = stage_record(stager)
    clock.advance(601)
    sw, err = stager.take(r["token"])
    assert sw is None and "expired" in err["error"]
    assert api.api_requests == []


def test_ttl_not_yet_expired(stager, clock):
    r = stage_record(stager)
    clock.advance(599)
    sw, err = stager.take(r["token"])
    assert err is None and sw is not None


def test_tampered_payload_is_refused(stager, client, api):
    r = stage_record(stager)
    sw, _ = stager.take(r["token"])
    sw.payload_json = sw.payload_json.replace("test", "evil")
    out = run(stager.execute(sw, client))
    assert out["status"] == "refused" and "integrity" in out["error"]
    assert api.api_requests == []


@pytest.mark.parametrize("service,method", [
    ("AuthenticationManager", "Login"),
    ("Scheduler", "GetRecordingRtmpInfo"),
    ("TaskManager", "GetSlackAccounts"),
    ("Configuration", "GetSettingAsString"),
    ("Library", "SaveVariant"),
])
def test_blocked_methods_cannot_be_staged(stager, service, method):
    r = stager.stage(service, method, {})
    assert r["status"] == "refused" and "blocked" in r["error"]
    assert stager.pending()["count"] == 0


def test_read_methods_cannot_be_staged(stager):
    r = stager.stage("Library", "GetMediaItems", {"index": 0, "count": 1})
    assert r["status"] == "refused" and "ss_call_read" in r["error"]


def test_unknown_method_and_params(stager):
    assert stager.stage("Library", "NoSuchMethod", {})["status"] == "refused"
    r = stager.stage("Scheduler", "RecordAiring", {"seriesID": GUID})
    assert r["status"] == "refused" and "unknown parameter" in r["error"]
    assert "seriesId: Guid" in r["valid_params"]


def test_cancel_and_pending(stager):
    r = stage_record(stager)
    assert stager.cancel(r["token"])["status"] == "canceled"
    assert stager.cancel(r["token"])["status"] == "refused"
    assert stager.pending()["count"] == 0


def test_audit_log_has_events_and_no_secrets(stager, client, settings):
    r = stager.stage(
        "Library", "SaveExportDestination",
        {"path": {"__type": "ExportFtp:#SnapStream.Services", "FriendlyName": "x", "Password": FAKE_CLIENT_SECRET}},
        reason="audit test",
    )
    assert r["status"] == "staged"
    assert FAKE_CLIENT_SECRET not in r["preview"]
    sw, _ = stager.take(r["token"])
    run(stager.execute(sw, client))
    text = settings.audit_file.read_text(encoding="utf-8")
    events = [json.loads(line)["event"] for line in text.splitlines()]
    assert events == ["staged", "commit_start", "commit_done"]
    assert no_secrets(text, extra=("tok-1-abcdefgh",))
    assert FAKE_API_KEY not in text


def test_commit_refused_when_audit_unwritable(catalog, client, tmp_path, clock, api):
    bad = tmp_path / "dir_not_file"
    bad.mkdir()
    st = WriteStager(catalog, bad, clock=clock)  # a directory cannot be appended to
    r = stage_record(st)
    assert r["warnings"]
    sw, _ = st.take(r["token"])
    out = run(st.execute(sw, client))
    assert out["status"] == "refused" and api.api_requests == []


def _audit(settings):
    return [json.loads(x) for x in settings.audit_file.read_text(encoding="utf-8").splitlines()]


def test_audit_lines_join_to_their_staged_write(stager, client, settings, api):
    a = stage_record(stager)
    b = stage_record(stager)  # same method, same payload
    assert a["payload_sha256"] == b["payload_sha256"] and a["stage_id"] != b["stage_id"]
    stager.cancel(a["token"])
    sw, _ = stager.take(b["token"])
    run(stager.execute(sw, client, elicit_mode="accepted"))
    lines = _audit(settings)
    assert [x["event"] for x in lines] == ["staged", "staged", "canceled", "commit_start", "commit_done"]
    for x in lines:
        assert "token" not in x  # the token itself is never logged
        assert x["method"] == "Scheduler/RecordAiring" and x["payload_sha256"] == a["payload_sha256"]
    by_event = {}
    for x in lines:
        by_event.setdefault(x["event"], []).append(x["stage_id"])
    assert by_event["staged"] == [a["stage_id"], b["stage_id"]]
    assert by_event["canceled"] == [a["stage_id"]]
    assert by_event["commit_start"] == by_event["commit_done"] == [b["stage_id"]]
    assert lines[3]["elicit_mode"] == "accepted"
    text = settings.audit_file.read_text(encoding="utf-8")
    assert a["token"] not in text and b["token"] not in text


def test_refusal_and_error_events_carry_hash(stager, client, settings, api, clock):
    import httpx

    expired = stage_record(stager)
    clock.advance(601)
    assert stager.take(expired["token"])[1]["status"] == "refused"
    api.routes[("Scheduler", "RecordAiring")] = httpx.Response(500, text="boom")
    failing = stage_record(stager)
    sw, _ = stager.take(failing["token"])
    assert run(stager.execute(sw, client))["status"] == "error"
    lines = {x["event"]: x for x in _audit(settings)}
    assert lines["commit_refused_expired"]["stage_id"] == expired["stage_id"]
    assert lines["commit_error"]["stage_id"] == failing["stage_id"]
    for ev in ("commit_refused_expired", "commit_error"):
        assert lines[ev]["payload_sha256"] == expired["payload_sha256"]


def test_credential_write_uses_strict_masking(stager, settings):
    r = stager.stage(
        "Library", "SaveExportDestination",
        {"path": {"__type": "ExportS3:#SnapStream.Services", "FriendlyName": "dest", "AccessKey": "AKVAL123", "SecretAccessKey": "SKVAL99"}},
    )
    r2 = stager.stage(
        "Library", "SaveExportDestination",
        {"path": {"__type": "ExportFtp:#SnapStream.Services", "FriendlyName": "dest2", "Username": "bob", "Password": "pw12345"}},
    )
    assert r["status"] == "staged" and r2["status"] == "staged"
    text = settings.audit_file.read_text(encoding="utf-8")
    for v in ("AKVAL123", "SKVAL99", "bob", "pw12345"):
        assert v not in r["preview"] and v not in r2["preview"] and v not in text
    assert '"FriendlyName": "dest"' in r["preview"]
    sw, _ = stager.take(r["token"])
    assert json.loads(sw.payload_json)["path"]["AccessKey"] == "AKVAL123"  # masked in display only


def test_ordinary_write_keeps_loose_masking(stager):
    r = stager.stage("Library", "AddPlayList", {"playList": {"Username": "bob"}})
    assert r["status"] == "staged" and "bob" in r["preview"]


# ------------------------------------------------------------- clip payloads
import base64  # noqa: E402

import httpx  # noqa: E402

from snapstream_mcp.staging import verify_hint  # noqa: E402

FP = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def clip_props(**over):
    cp = {
        "ClipPropertiesType": 1, "SourcePathId": FP, "SourceFile": "news.mpg", "ItemId": GUID,
        "StartPosition": 620_000_000, "EndPosition": 755_000_000, "ClipTitle": "Clip", "DestinationFolderId": GUID,
    }
    cp.update(over)
    return {"clipProperties": cp}


@pytest.fixture
def cat_client(cfg, settings, api, clock, catalog):
    """A client wired to the catalog, so it routes by url_path and adds the authTicket stub."""
    return SnapStreamClient(cfg, settings, transport=api.transport(), clock=clock, sleep=clock.sleep, catalog=catalog)


def test_clip_stage_preview_and_commit_sends_auth_ticket_stub(stager, cat_client, api, settings):
    r = stager.stage("TaskManager", "ClipFile", clip_props(), reason="clip test")
    assert r["status"] == "staged", r
    p = r["preview"]
    assert "Clip in:     00:01:02" in p and "Clip out:    00:01:15.500" in p and "Clip length: 00:00:13.500" in p
    assert '"authTicket": {} is added when sending' in p
    assert "single SnapStream user mapped to this API client ID" in p
    sw, _ = stager.take(r["token"])
    assert "authTicket" not in json.loads(sw.payload_json)  # the stub is added by the client, not frozen
    api.routes[("TaskManager", "ClipFile")] = httpx.Response(
        200, json={"ClipFileResult": GUID}, headers={"x-amzn-RequestId": "req-clip-1"}
    )
    out = run(stager.execute(sw, cat_client))
    assert out["status"] == "committed" and out["request_id"] == "req-clip-1"
    sent = api.bodies()[-1]
    assert sent["authTicket"] == {} and sent["clipProperties"]["StartPosition"] == 620_000_000
    done = [json.loads(x) for x in settings.audit_file.read_text(encoding="utf-8").splitlines() if '"commit_done"' in x]
    assert done[0]["request_id"] == "req-clip-1"


def test_clip_stage_note_when_stub_disabled(catalog, settings, clock):
    st = WriteStager(catalog, settings.audit_file, clock=clock, empty_auth_ticket=False)
    r = st.stage("TaskManager", "ClipFile", clip_props())
    assert r["status"] == "staged" and "authTicket is omitted" in r["preview"]


@pytest.mark.parametrize("over,needle", [
    ({"StartPosition": 62000, "EndPosition": 75500}, "did you send milliseconds"),
    ({"StartPosition": 62.0, "EndPosition": 75.5}, "must both be integers"),
    ({"StartPosition": 755_000_000, "EndPosition": 620_000_000}, "less than EndPosition"),
])
def test_clip_stage_refuses_bad_positions(stager, over, needle):
    r = stager.stage("TaskManager", "ClipFile", clip_props(**over))
    assert r["status"] == "refused" and needle in r["error"] and "100 ns ticks" in r["error"]
    assert stager.pending()["count"] == 0


def test_clip_stage_refuses_wrong_case_and_unknown_fields(stager):
    body = clip_props()
    body["clipProperties"]["subtitles"] = True
    r = stager.stage("TaskManager", "AiClipDesigner", body)
    assert r["status"] == "refused" and "did you mean Subtitles" in r["error"]
    body = clip_props(Orientationn=1)
    r = stager.stage("TaskManager", "AiClipDesigner", body)
    assert r["status"] == "refused" and "unknown field(s) Orientationn" in r["error"]
    ok = clip_props(Subtitles=True, Orientation=1, Layout=0)
    assert stager.stage("TaskManager", "AiClipDesigner", ok)["status"] == "staged"


def test_existing_clip_methods_need_no_positions(stager):
    r = stager.stage("TaskManager", "DesignExistingClip", {"clipProperties": {"ClipPropertiesType": 1, "ExistingItemId": GUID}})
    assert r["status"] == "staged", r


# ------------------------------------------------------------ commit outcomes
def _b64(text):
    return base64.b64encode(text.encode()).decode()


def test_fault_on_200_is_not_committed(stager, cat_client, api, settings):
    api.routes[("TaskManager", "ClipFile")] = httpx.Response(
        200, content=b"", headers={"CustomError": _b64("Object reference not set to an instance of an object."), "x-amzn-RequestId": "rf"}
    )
    r = stager.stage("TaskManager", "ClipFile", clip_props())
    sw, _ = stager.take(r["token"])
    out = run(stager.execute(sw, cat_client))
    assert out["status"] == "error" and out["kind"] == "api_fault" and out["http_status"] == 200
    # A fault from the method itself may come after part of the write ran: no definite "Not committed".
    assert out["request_id"] == "rf" and "Not committed" not in out["note"] and "may have run" in out["note"]
    assert "GetTaskLists" in out["verify_hint"]
    events = [json.loads(x) for x in settings.audit_file.read_text(encoding="utf-8").splitlines()]
    assert [e["event"] for e in events] == ["staged", "commit_start", "commit_error"]
    assert events[-1]["kind"] == "api_fault" and events[-1]["request_id"] == "rf"


def test_xml_fault_on_200_is_not_committed(stager, cat_client, api):
    api.routes[("TaskManager", "ClipFile")] = httpx.Response(
        200, text="<s:Envelope><s:Body><s:Fault><faultstring>Boom</faultstring></s:Fault></s:Body></s:Envelope>"
    )
    r = stager.stage("TaskManager", "ClipFile", clip_props())
    sw, _ = stager.take(r["token"])
    out = run(stager.execute(sw, cat_client))
    assert out["status"] == "error" and out["kind"] == "api_fault"


def _raise(exc):
    def handler(request):
        raise exc
    return handler


@pytest.mark.parametrize("route,method,status,hint", [
    (_raise(httpx.ReadTimeout("slow")), "ClipFile", "outcome_unknown", "GetTaskLists"),
    (_raise(httpx.RemoteProtocolError("dropped")), "ClipFile", "outcome_unknown", "ss_get_job"),
    (httpx.Response(504), "RecordAiring", "outcome_unknown", "GetRecordingJobs"),
    (httpx.Response(502), "RecordAiring", "outcome_unknown", "GetCurrentAndUpcomingRecordings"),
    (httpx.Response(503), "SaveBookmark", "outcome_unknown", "Re-read the object before retrying."),
    (_raise(httpx.ConnectError("refused")), "SaveBookmark", "error", None),
    (_raise(httpx.ConnectTimeout("refused")), "SaveBookmark", "error", None),
    (httpx.Response(500), "SaveBookmark", "error", "Re-read the object before retrying."),
    (httpx.Response(400), "SaveBookmark", "error", None),
], ids=["read_timeout", "dropped", "504", "502", "503", "connect_error", "connect_timeout", "500", "400"])
def test_commit_outcome_classification(stager, cat_client, api, settings, route, method, status, hint):
    svc = "Scheduler" if method == "RecordAiring" else ("TaskManager" if method == "ClipFile" else "Library")
    params = {
        "ClipFile": clip_props(),
        "RecordAiring": {"seriesId": GUID, "airingId": GUID},
        "SaveBookmark": {"item": {"ClipTitle": "x"}},
    }[method]
    api.routes[(svc, method)] = route
    r = stager.stage(svc, method, params)
    sw, _ = stager.take(r["token"])
    out = run(stager.execute(sw, cat_client))
    assert out["status"] == status, out
    assert len(api.api_requests) == 1  # never retried
    events = [json.loads(x)["event"] for x in settings.audit_file.read_text(encoding="utf-8").splitlines()]
    if status == "outcome_unknown":
        assert hint in out["verify_hint"] and hint in out["note"] and "NOT retried" in out["note"]
        assert events[-1] == "commit_unknown"
    elif hint:  # HTTP 5xx: part of the write may have run
        assert hint in out["verify_hint"] and "Not committed" not in out["note"] and events[-1] == "commit_error"
    else:  # certainly not sent, or refused before the method ran
        assert "verify_hint" not in out and "Not committed" in out["note"] and events[-1] == "commit_error"
    assert "commit_done" not in events


def test_verify_hint_mapping():
    assert "GetTaskLists" in verify_hint("AiClipDesigner") and "ss_get_job" in verify_hint("DesignExistingClip")
    assert "GetRecordingJobs" in verify_hint("RecordShow")
    assert verify_hint("SaveBookmark") == "Re-read the object before retrying."
    # Every task-list creator, the "Check with <read>" wording, and alerts.
    for m in ("TranscribeRecording", "ClipAndSlack", "ExportFile", "ClipFile"):
        assert verify_hint(m).startswith("Check with TaskManager/GetTaskLists"), m
    assert verify_hint("RecordAiring").startswith("Check with Scheduler/GetRecordingJobs")
    assert "NotifierManager/GetMyAlerts" in verify_hint("AddAlert")
    assert verify_hint("AddTaskAction") == "Re-read the object before retrying."  # returns an action id, not a task list


def _events(settings):
    return [json.loads(x) for x in settings.audit_file.read_text(encoding="utf-8").splitlines()]


def _commit(stager, client, service, method, params):
    r = stager.stage(service, method, params)
    assert r["status"] == "staged", r
    sw, _ = stager.take(r["token"])
    return run(stager.execute(sw, client))


# ------------------------------------------------- Guid results must be a GUID (WS-1)
@pytest.mark.parametrize("response", [
    httpx.Response(200, content=b""),  # client returns None
    httpx.Response(200, json={"ClipFileResult": "00000000-0000-0000-0000-000000000000"}),  # Guid.Empty
    httpx.Response(200, json={"ClipFileResult": None}),
    httpx.Response(200, json={"ClipFileResult": []}),
    httpx.Response(200, text="oops", headers={"content-type": "text/plain"}),  # {"_non_json_body": ...}
], ids=["empty_body", "empty_guid", "null", "list", "non_json"])
def test_guid_write_without_a_guid_is_not_committed(stager, cat_client, api, settings, response):
    api.routes[("TaskManager", "ClipFile")] = response
    out = _commit(stager, cat_client, "TaskManager", "ClipFile", clip_props())
    assert out["status"] == "error" and out["kind"] == "unexpected_result", out
    assert "GetTaskLists" in out["verify_hint"] and "NOT retried" in out["note"]
    events = [e["event"] for e in _events(settings)]
    assert events[-1] == "commit_error" and "commit_done" not in events


@pytest.mark.parametrize("payload", [{"ClipFileResult": GUID}, GUID], ids=["wrapped", "bare"])
def test_guid_write_with_a_guid_is_committed(stager, cat_client, api, settings, payload):
    api.routes[("TaskManager", "ClipFile")] = httpx.Response(200, json=payload)
    out = _commit(stager, cat_client, "TaskManager", "ClipFile", clip_props())
    assert out["status"] == "committed", out
    assert _events(settings)[-1]["event"] == "commit_done"


# ------------------------------------------------------ fault classification (WS-2)
def test_record_airing_already_scheduled_is_already_done(stager, cat_client, api, settings):
    api.routes[("Scheduler", "RecordAiring")] = httpx.Response(
        200, content=b"", headers={"CustomError": _b64("This airing is already scheduled to record.")}
    )
    out = _commit(stager, cat_client, "Scheduler", "RecordAiring", {"seriesId": GUID, "airingId": GUID})
    assert out["status"] == "already_done", out
    assert "GetRecordingJobs" in out["verify_hint"] and "already scheduled" in out["note"]
    assert _events(settings)[-1]["event"] == "commit_already_done"


def test_permission_inspector_fault_is_definitely_not_committed(stager, cat_client, api):
    trace = (
        "Object reference not set to an instance of an object.\n"
        "   at SnapStream.Gateway.GatewayTicketPermissionInspector.AfterReceiveRequest(Message& request)"
    )
    api.routes[("TaskManager", "ClipFile")] = httpx.Response(200, content=b"", headers={"CustomError": _b64(trace)})
    out = _commit(stager, cat_client, "TaskManager", "ClipFile", clip_props())
    assert out["status"] == "error" and out["kind"] == "api_fault"
    assert "Not committed" in out["note"] and "verify_hint" not in out


def test_wait_for_rebuild_warning_in_preview(stager):
    r = stage_record(stager)  # waitForRebuild True
    assert "waitForRebuild is true" in r["preview"] and "outcome_unknown" in r["preview"]
    r2 = stager.stage("Scheduler", "RecordAiring", {"seriesId": GUID, "airingId": GUID, "waitForRebuild": False})
    assert "waitForRebuild is true" not in r2["preview"]


# ----------------------------------------------------- __type goes first (api-1)
def test_subtype_hint_is_sent_first(stager, cat_client, api):
    # The caller lists __type last; WCF only honors it as the first member.
    path = {"FriendlyName": "dest", "Username": "bob", "__type": "ExportFtp:#SnapStream.Services"}
    r = stager.stage("Library", "AddExportDestination", {"path": path})
    assert r["status"] == "staged", r
    sw, _ = stager.take(r["token"])
    run(stager.execute(sw, cat_client))
    req = [q for q in api.api_requests if q.url.path.endswith("/AddExportDestination")][0]
    sent = json.loads(req.content, object_pairs_hook=lambda pairs: pairs)
    nested = dict(sent)["path"]
    assert nested[0][0] == "__type", nested
    assert payload_hash(json.loads(req.content)) == r["payload_sha256"]  # hash is order-independent
    # The preview shows it first too.
    assert r["preview"].index('"__type"') < r["preview"].index('"FriendlyName"')


# ------------------------------------------------ cancellation mid-commit (R1)
class _RaisingClient:
    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    async def call(self, *a, **kw):
        self.calls += 1
        raise self.exc


def test_canceled_commit_closes_the_audit_trail(stager, settings):
    r = stage_record(stager)
    sw, _ = stager.take(r["token"])
    with pytest.raises(asyncio.CancelledError):
        run(stager.execute(sw, _RaisingClient(asyncio.CancelledError())))
    events = _events(settings)
    assert [e["event"] for e in events] == ["staged", "commit_start", "commit_unknown"]
    assert events[-1]["kind"] == "canceled" and "GetRecordingJobs" in events[-1]["verify_hint"]


def test_unexpected_exception_is_outcome_unknown(stager, settings):
    r = stage_record(stager)
    sw, _ = stager.take(r["token"])
    out = run(stager.execute(sw, _RaisingClient(RuntimeError("boom"))))
    assert out["status"] == "outcome_unknown" and out["kind"] == "RuntimeError"
    assert "GetRecordingJobs" in out["verify_hint"]
    assert _events(settings)[-1]["event"] == "commit_unknown"
