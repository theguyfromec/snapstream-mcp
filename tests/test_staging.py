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
    r = stager.stage("Library", "SaveExportDestination", {"path": {"Name": "x", "Password": FAKE_CLIENT_SECRET}}, reason="audit test")
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
        {"path": {"Name": "dest", "AccountKey": "AKVAL123", "StreamKey": "SKVAL99", "UserName": "bob"}},
    )
    assert r["status"] == "staged"
    text = settings.audit_file.read_text(encoding="utf-8")
    for v in ("AKVAL123", "SKVAL99", "bob"):
        assert v not in r["preview"] and v not in text
    assert '"Name": "dest"' in r["preview"]
    sw, _ = stager.take(r["token"])
    assert json.loads(sw.payload_json)["path"]["AccountKey"] == "AKVAL123"  # masked in display only


def test_ordinary_write_keeps_loose_masking(stager):
    r = stager.stage("Library", "SaveBookmark", {"item": {"Title": "Clip", "UserName": "bob"}})
    assert r["status"] == "staged" and "bob" in r["preview"]
