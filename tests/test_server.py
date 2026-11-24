import json

import httpx
import pytest

from snapstream_mcp import server

from .conftest import FAKE_API_KEY, FAKE_CLIENT_SECRET, dump, no_secrets, run

GUID = "3fa85f64-5717-4562-b3fc-2c963f66afa6"


def test_tools_registered():
    names = {t.name for t in run(server.mcp.list_tools())}
    for n in (
        "ss_status", "ss_api_catalog", "ss_api_describe", "ss_call_read", "ss_stage_write",
        "ss_commit_write", "ss_cancel_staged", "ss_pending_writes", "ss_search_recordings",
        "ss_list_media_items", "ss_get_media_item", "ss_get_transcript", "ss_get_smart_chapters",
        "ss_list_clips_and_bookmarks", "ss_list_channels", "ss_guide_for_channel", "ss_search_guide",
        "ss_list_scheduled_recordings",
    ):
        assert n in names
    commit = next(t for t in run(server.mcp.list_tools()) if t.name == "ss_commit_write")
    assert commit.annotations.destructive_hint is True
    assert commit.meta == {"anthropic/requiresUserInteraction": True}


@pytest.mark.parametrize("service,method,access", [
    ("Library", "DeleteMediaItem", "write"),
    ("TaskManager", "ClipFile", "write"),
    ("AuthenticationManager", "Login", "blocked"),
    ("Scheduler", "GetRecordingRtmpInfo", "blocked"),
    ("TaskManager", "GetYouTubeAccounts", "blocked"),
])
def test_call_read_refuses_write_and_blocked(rt, api, service, method, access):
    out = run(server.ss_call_read(service, method, {}))
    assert "error" in out and out["access"] == access
    if access == "write":
        assert "ss_stage_write" in out["error"]
    assert api.requests == []  # nothing reached the network, not even a token request


def test_call_read_unknown_method(rt, api):
    out = run(server.ss_call_read("Library", "DropEverything", {}))
    assert "Unknown method" in out["error"] and api.requests == []


def test_call_read_normalizes(rt, api):
    api.routes[("Library", "GetMediaItems")] = {"GetMediaItemsResult": [
        {"Id": GUID, "Title": "News", "Start": "/Date(1795552679779-0600)/", "ParentId": "00000000-0000-0000-0000-000000000000", "Length": "PT1M"}
    ]}
    out = run(server.ss_call_read("Library", "GetMediaItems"))
    assert api.bodies()[-1] == {"index": 0, "count": 50}  # paging defaults
    item = out["result"][0]
    assert item == {"Id": GUID, "Title": "News", "Start": "2026-11-24T20:37:59.779Z", "Length": "PT1M", "Length_seconds": 60}
    assert out["truncated"] is False


def test_call_read_bad_params(rt, api):
    out = run(server.ss_call_read("Library", "GetMediaItem", {"id": GUID}))
    assert "unknown parameter" in out["error"] and "itemId: Guid" in out["valid_params"]
    assert api.requests == []


def test_call_read_error_is_redacted(rt, api):
    api.routes[("Library", "GetMediaItem")] = httpx.Response(500, text=f"leak {FAKE_API_KEY} {FAKE_CLIENT_SECRET}")
    out = run(server.ss_call_read("Library", "GetMediaItem", {"itemId": GUID}))
    assert out["http_status"] == 500
    assert no_secrets(dump(out))


CURATED = [
    (server.ss_search_recordings, {"search": "election"}, ("Search", "SearchRecordings")),
    (server.ss_count_recording_matches, {"search": "x"}, ("Search", "SearchRecordingsCount")),
    (server.ss_search_in_recording, {"search": "x", "mediaItem": GUID}, ("Search", "SearchRecordingWithRefinement")),
    (server.ss_search_guide, {"search": "x"}, ("Search", "SearchAirings")),
    (server.ss_list_media_items, {}, ("Library", "GetMediaItems")),
    (server.ss_get_media_item, {"itemId": GUID}, ("Library", "GetMediaItem")),
    (server.ss_get_transcript, {"itemId": GUID}, ("Library", "GetTranscript")),
    (server.ss_get_smart_chapters, {"itemId": GUID}, ("Library", "GetSmartChapters")),
    (server.ss_list_clips_and_bookmarks, {"parentId": GUID}, ("Library", "GetClipsAndBookmarks")),
    (server.ss_get_bookmark, {"itemId": GUID}, ("Library", "GetBookmarkById")),
    (server.ss_list_library_channels, {}, ("Library", "GetAllPastAndCurrentChannels")),
    (server.ss_storage_status, {}, ("Library", "GetDrives")),
    (server.ss_list_channels, {}, ("Scheduler", "GetChannels")),
    (server.ss_whats_on, {"channelIds": [GUID]}, ("Scheduler", "GetAiringsAtTime")),
    (server.ss_guide_for_channel, {"channelId": GUID}, ("Scheduler", "GetAiringsByChannelAndTimeRange")),
    (server.ss_guide_by_time, {}, ("Scheduler", "GetAiringsByStartTimeRange")),
    (server.ss_get_airing, {"id": GUID}, ("Scheduler", "GetAiring")),
    (server.ss_list_scheduled_recordings, {}, ("Scheduler", "GetCurrentAndUpcomingRecordings")),
    (server.ss_list_current_recordings, {}, ("Scheduler", "GetCurrentRecordings")),
    (server.ss_list_recent_recordings, {}, ("Scheduler", "GetRecentRecordings")),
    (server.ss_list_recording_jobs, {}, ("Scheduler", "GetRecordingJobs")),
    (server.ss_get_job, {"listId": GUID}, ("TaskManager", "GetTaskList")),
    (server.ss_whoami, {}, ("AuthenticationManager", "GetUserEmail")),
]


@pytest.mark.parametrize("fn,kwargs,target", CURATED, ids=[c[0].__name__ for c in CURATED])
def test_curated_tools_use_read_methods_and_exact_param_names(rt, api, catalog, fn, kwargs, target):
    out = run(fn(**kwargs))
    assert "error" not in out, out
    info = catalog.get(*target)
    assert info.access == "read"
    req = api.api_requests[-1]
    assert req.url.path.endswith(f"/{target[0]}/json/{target[1]}")
    sent = json.loads(req.content)
    assert set(sent) <= set(info.param_names), (set(sent) - set(info.param_names))
    for k, v in kwargs.items():
        if k in info.param_names:
            assert k in sent


@pytest.mark.parametrize("fn,arg,expected", [
    (server.ss_search_recordings, None, 4),
    (server.ss_search_recordings, "program_info", 2),
    (server.ss_search_recordings, "ALL", 255),
    (server.ss_search_recordings, 6, 6),
    (server.ss_search_recordings, "6", 6),
    (server.ss_count_recording_matches, None, 4),
    (server.ss_search_guide, None, 255),
    (server.ss_search_guide, 1, 1),
])
def test_search_type_sent_as_integer_flag(rt, api, fn, arg, expected):
    # The live API rejects enum names such as "Default" with HTTP 400; it wants the integer.
    kwargs = {"search": "x"} if arg is None else {"search": "x", "type": arg}
    out = run(fn(**kwargs))
    assert "error" not in out, out
    sent = api.bodies()[-1]["type"]
    assert sent == expected and isinstance(sent, int) and not isinstance(sent, bool)


@pytest.mark.parametrize("bad", ["Default", "captionz", True])
def test_search_type_rejects_unknown_names_without_calling(rt, api, bad):
    out = run(server.ss_search_recordings(search="x", type=bad))
    assert out["kind"] == "invalid_argument"
    assert api.api_requests == []


def test_scheduler_status_combines_three_reads(rt, api):
    out = run(server.ss_scheduler_status())
    assert set(out) == {"GetStats", "GetUpdateStatus", "GetLastRebuild"}
    paths = [r.url.path.rsplit("/", 1)[-1] for r in api.api_requests]
    assert paths == ["GetStats", "GetUpdateStatus", "GetLastRebuild"]


def test_guide_dates_sent_in_wcf_form(rt, api):
    run(server.ss_guide_for_channel(GUID, rangeBegin="1970-01-01T00:00:01Z", rangeEnd="1970-01-01T00:00:02Z"))
    body = api.bodies()[-1]
    assert body["rangeBegin"] == "/Date(1000)/" and body["rangeEnd"] == "/Date(2000)/"
    assert body["channelId"] == GUID and body["index"] == 0 and body["count"] == 200


def test_transcript_windowing(rt, api):
    caps = [{"Start": f"/Date({1_000_000 + i * 2000})/", "Text": f"line {i} hello" if i % 3 == 0 else f"line {i}"} for i in range(200)]
    api.routes[("Library", "GetTranscript")] = {"GetTranscriptResult": caps}
    first = run(server.ss_get_transcript(GUID, max_chars=500))
    assert first["caption_count"] == 200
    assert first["transcript"].startswith("[00:00:00] line 0 hello")
    assert first["truncated"] is True and first["next_start_caption"] > 0
    nxt = run(server.ss_get_transcript(GUID, start_caption=first["next_start_caption"], max_chars=500))
    assert nxt["transcript"].split("\n")[0].endswith(f"line {first['next_start_caption']}") or "hello" in nxt["transcript"].split("\n")[0]
    by_time = run(server.ss_get_transcript(GUID, offset_seconds=100, max_chars=500))
    assert by_time["transcript"].startswith("[00:01:40] line 50")
    filtered = run(server.ss_get_transcript(GUID, contains="HELLO", max_chars=100000))
    assert filtered["returned_lines"] == 67 and filtered["truncated"] is False


def test_status_reports_without_secrets(rt, api):
    out = run(server.ss_status())
    assert out["config"]["ok"] is True and out["token"]["ok"] is True
    assert out["base_host"] == "api.example.test" and out["instance"] == "test.instance"
    assert out["catalog"] == {"services": 12, "methods": 461, "read": 246, "write": 159, "blocked": 56}
    assert no_secrets(dump(out), extra=("tok-1-abcdefgh", "oauth2/token"))


def test_status_without_config(api):
    out = run(server.ss_status())  # the autouse guard points at a missing secrets file
    assert out["config"]["ok"] is False and "not found" in out["config"]["error"]
    assert api.requests == []


def test_stage_and_commit_through_tools(rt, api):
    staged = server.ss_stage_write("Library", "SaveBookmark", {"item": {"Title": "Clip A", "ParentId": GUID}}, reason="test")
    assert staged["status"] == "staged"
    assert server.ss_pending_writes()["count"] == 1
    out = run(server.ss_commit_write(staged["token"], None))
    assert out["status"] == "committed"
    assert api.bodies()[-1] == {"item": {"Title": "Clip A", "ParentId": GUID}}
    again = run(server.ss_commit_write(staged["token"], None))
    assert again["status"] == "refused"
    assert len(api.api_requests) == 1


def test_commit_via_mcp_call_tool_without_elicitation(rt, api):
    staged = server.ss_stage_write("Library", "SaveBookmark", {"item": {"Title": "B"}})
    res = run(server.mcp.call_tool("ss_commit_write", {"token": staged["token"]}))
    payload = res.structured_content["result"]
    assert payload["status"] == "committed"


def test_stage_tool_refuses_blocked_and_read(rt):
    assert server.ss_stage_write("AuthenticationManager", "Login", {})["status"] == "refused"
    assert server.ss_stage_write("Library", "GetMediaItems", {})["status"] == "refused"


def test_api_catalog_and_describe(rt):
    overview = server.ss_api_catalog()
    assert len(overview["services"]) == 12 and overview["totals"]["methods"] == 461
    found = server.ss_api_catalog(query="transcript", access="read")
    assert any(m["method"] == "GetTranscript" for m in found["matches"])
    d = server.ss_api_describe("library", "gettranscript")
    assert d["params"] == [{"name": "itemId", "type": "Guid"}] and d["access"] == "read"


# ------------------------------------------------------------ commit confirmation
from mcp.server.elicitation import AcceptedElicitation, CancelledElicitation, DeclinedElicitation  # noqa: E402
from mcp.types import ClientCapabilities, ElicitationCapability  # noqa: E402

from .conftest import FAKE_CLIENT_ID  # noqa: E402


class FakeCtx:
    """Stands in for the MCP request Context: declared capabilities plus a scripted elicit()."""

    def __init__(self, reply=None, *, raises=None, elicitation=True):
        self.client_capabilities = ClientCapabilities(elicitation=ElicitationCapability() if elicitation else None)
        self.reply = reply
        self.raises = raises
        self.calls = 0

    async def elicit(self, message, schema):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.reply


def _audit_lines(rt):
    return [json.loads(x) for x in rt.settings.audit_file.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("ctx_factory,mode", [
    (lambda: FakeCtx(DeclinedElicitation()), "declined"),
    (lambda: FakeCtx(CancelledElicitation()), "canceled"),
    (lambda: FakeCtx(AcceptedElicitation(data=server._ConfirmWrite(confirm=False))), "declined"),
    (lambda: FakeCtx(raises=ValueError("Received an accepted elicitation whose content does not match the requested schema")), "error:ValueError"),
    (lambda: FakeCtx(raises=TimeoutError("no reply")), "error:TimeoutError"),
    (lambda: FakeCtx(raises=RuntimeError("transport closed")), "error:RuntimeError"),
], ids=["declined", "canceled", "accept_confirm_false", "accept_bad_schema", "timeout", "transport_error"])
def test_commit_fails_closed_unless_confirmed(rt, api, ctx_factory, mode):
    staged = server.ss_stage_write("Library", "SaveBookmark", {"item": {"Title": "C"}})
    ctx = ctx_factory()
    out = run(server.ss_commit_write(staged["token"], ctx))
    assert ctx.calls == 1
    assert out["status"] == "aborted" and out["elicit_mode"] == mode
    assert api.api_requests == []  # nothing was sent
    assert server.ss_pending_writes()["count"] == 0
    assert run(server.ss_commit_write(staged["token"], None))["status"] == "refused"  # token is spent
    assert api.api_requests == []
    declined = [x for x in _audit_lines(rt) if x["event"] == "commit_declined"]
    assert len(declined) == 1 and declined[0]["elicit_mode"] == mode
    assert declined[0]["stage_id"] == staged["stage_id"] and declined[0]["payload_sha256"] == staged["payload_sha256"]


def test_commit_proceeds_when_confirmed(rt, api):
    staged = server.ss_stage_write("Library", "SaveBookmark", {"item": {"Title": "D"}})
    ctx = FakeCtx(AcceptedElicitation(data=server._ConfirmWrite(confirm=True)))
    out = run(server.ss_commit_write(staged["token"], ctx))
    assert out["status"] == "committed" and out["confirmation"] == "accepted"
    assert len(api.api_requests) == 1
    start = next(x for x in _audit_lines(rt) if x["event"] == "commit_start")
    assert start["elicit_mode"] == "accepted" and start["stage_id"] == staged["stage_id"]


def test_commit_without_declared_elicitation_proceeds_on_token(rt, api):
    staged = server.ss_stage_write("Library", "SaveBookmark", {"item": {"Title": "E"}})
    ctx = FakeCtx(raises=AssertionError("must not be asked"), elicitation=False)
    out = run(server.ss_commit_write(staged["token"], ctx))
    assert ctx.calls == 0
    assert out["status"] == "committed" and out["confirmation"] == "unsupported"
    start = next(x for x in _audit_lines(rt) if x["event"] == "commit_start")
    assert start["elicit_mode"] == "unsupported"


# ------------------------------------------------------- success-path redaction
def test_successful_read_output_is_scrubbed(rt, api):
    run(server.ss_status())  # mint tok-1 so the live token is known to the redactor
    msg = f"client {FAKE_CLIENT_ID} key {FAKE_API_KEY} tok tok-1-abcdefgh link https://h/live?token=abc123&X-Amz-Signature=deadbeef"
    api.routes[("Logger", "GetLogEntries")] = {"GetLogEntriesResult": [{"Message": msg}]}
    out = run(server.ss_call_read("Logger", "GetLogEntries", {}))
    text = dump(out)
    assert "error" not in out, out
    assert no_secrets(text, extra=("tok-1-abcdefgh", "abc123", "deadbeef"))
    assert "[REDACTED]" in text


def test_transcript_output_is_scrubbed_but_prose_kept(rt, api):
    run(server.ss_status())
    caps = [
        {"Start": "/Date(1000000)/", "Text": f"secret is {FAKE_API_KEY} and tok-1-abcdefgh"},
        {"Start": "/Date(1002000)/", "Text": "the bearer of bad news"},
    ]
    api.routes[("Library", "GetTranscript")] = {"GetTranscriptResult": caps}
    out = run(server.ss_get_transcript(GUID))
    assert no_secrets(dump(out), extra=("tok-1-abcdefgh",))
    assert "the bearer of bad news" in out["transcript"]


def test_commit_result_is_scrubbed(rt, api):
    api.routes[("Library", "SaveBookmark")] = {"SaveBookmarkResult": f"ok {FAKE_CLIENT_ID} https://h/x?sig=s3cr3tsig"}
    staged = server.ss_stage_write("Library", "SaveBookmark", {"item": {"Title": "F"}})
    out = run(server.ss_commit_write(staged["token"], None))
    assert out["status"] == "committed"
    assert no_secrets(dump(out), extra=("s3cr3tsig",))


def test_stream_link_method_is_blocked(rt, api):
    out = run(server.ss_call_read("StreamScheduler", "GetStreamLinkForChannel", {}))
    assert out["access"] == "blocked"
    assert server.ss_stage_write("StreamScheduler", "GetStreamLinkForChannel", {})["status"] == "refused"
    assert api.requests == []
