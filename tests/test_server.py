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
        "ss_list_scheduled_recordings", "ss_stage_clip",
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
    (server.ss_search_recordings, None, 254),
    (server.ss_search_recordings, "default", 254),
    (server.ss_search_recordings, "Default", 254),
    (server.ss_search_recordings, "captions", 4),
    (server.ss_search_recordings, "title", 2),
    (server.ss_search_recordings, "description", 1),
    (server.ss_search_recordings, "advanced_description", 8),
    (server.ss_search_recordings, "tags", 16),
    (server.ss_search_recordings, "as_run", 32),
    (server.ss_search_recordings, "speaker", 64),
    (server.ss_search_recordings, "ai_chapters", 128),
    (server.ss_search_recordings, "Program_Info", 58),
    (server.ss_search_recordings, "ALL", 255),
    (server.ss_search_recordings, "title|captions", 6),
    (server.ss_search_recordings, ["title", "tags", 32], 50),
    (server.ss_search_recordings, 6, 6),
    (server.ss_search_recordings, "6", 6),
    (server.ss_count_recording_matches, None, 254),
    (server.ss_count_recording_matches, "program_info", 58),
    (server.ss_search_guide, None, 7),
    (server.ss_search_guide, "people", 4),
    (server.ss_search_guide, ["title", "description"], 3),
    (server.ss_search_guide, 1, 1),
])
def test_search_type_sent_as_integer_flag(rt, api, fn, arg, expected):
    # The live API rejects enum names such as "Default" with HTTP 400; it wants the integer.
    kwargs = {"search": "x"} if arg is None else {"search": "x", "type": arg}
    out = run(fn(**kwargs))
    assert "error" not in out, out
    sent = api.bodies()[-1]["type"]
    assert sent == expected and isinstance(sent, int) and not isinstance(sent, bool)


@pytest.mark.parametrize("bad", ["Defaultx", "captionz", True, [], ["title", "nope"]])
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
    assert out["catalog"] == {"services": 12, "methods": 479, "read": 249, "write": 162, "blocked": 68}
    assert no_secrets(dump(out), extra=("tok-1-abcdefgh", "oauth2/token"))


def test_status_without_config(api):
    out = run(server.ss_status())  # the autouse guard points at a missing secrets file
    assert out["config"]["ok"] is False and "not found" in out["config"]["error"]
    assert api.requests == []


def test_stage_and_commit_through_tools(rt, api):
    staged = server.ss_stage_write("Library", "SaveBookmark", {"item": {"ClipTitle": "Clip A", "Id": GUID}}, reason="test")
    assert staged["status"] == "staged"
    assert server.ss_pending_writes()["count"] == 1
    out = run(server.ss_commit_write(staged["token"], None))
    assert out["status"] == "committed"
    assert api.bodies()[-1] == {"item": {"ClipTitle": "Clip A", "Id": GUID}}
    again = run(server.ss_commit_write(staged["token"], None))
    assert again["status"] == "refused"
    assert len(api.api_requests) == 1


def test_commit_via_mcp_call_tool_without_elicitation(rt, api):
    staged = server.ss_stage_write("Library", "SaveBookmark", {"item": {"ClipTitle": "B"}})
    res = run(server.mcp.call_tool("ss_commit_write", {"token": staged["token"]}))
    payload = res.structured_content["result"]
    assert payload["status"] == "committed"


def test_stage_tool_refuses_blocked_and_read(rt):
    assert server.ss_stage_write("AuthenticationManager", "Login", {})["status"] == "refused"
    assert server.ss_stage_write("Library", "GetMediaItems", {})["status"] == "refused"


def test_api_catalog_and_describe(rt):
    overview = server.ss_api_catalog()
    assert len(overview["services"]) == 12 and overview["totals"]["methods"] == 479
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
    staged = server.ss_stage_write("Library", "SaveBookmark", {"item": {"ClipTitle": "C"}})
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
    staged = server.ss_stage_write("Library", "SaveBookmark", {"item": {"ClipTitle": "D"}})
    ctx = FakeCtx(AcceptedElicitation(data=server._ConfirmWrite(confirm=True)))
    out = run(server.ss_commit_write(staged["token"], ctx))
    assert out["status"] == "committed" and out["confirmation"] == "accepted"
    assert len(api.api_requests) == 1
    start = next(x for x in _audit_lines(rt) if x["event"] == "commit_start")
    assert start["elicit_mode"] == "accepted" and start["stage_id"] == staged["stage_id"]


def test_commit_without_declared_elicitation_proceeds_on_token(rt, api):
    staged = server.ss_stage_write("Library", "SaveBookmark", {"item": {"ClipTitle": "E"}})
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
    staged = server.ss_stage_write("Library", "SaveBookmark", {"item": {"ClipTitle": "F"}})
    out = run(server.ss_commit_write(staged["token"], None))
    assert out["status"] == "committed"
    assert no_secrets(dump(out), extra=("s3cr3tsig",))


def test_stream_link_method_is_blocked(rt, api):
    out = run(server.ss_call_read("StreamScheduler", "GetStreamLinkForChannel", {}))
    assert out["access"] == "blocked"
    assert server.ss_stage_write("StreamScheduler", "GetStreamLinkForChannel", {})["status"] == "refused"
    assert api.requests == []


# ------------------------------------------------------- service aliases, describe
import base64  # noqa: E402

from .conftest import CHANNEL_ID, FOLDER_ID, FOLDER_PATH_ID, ITEM_ID  # noqa: E402


def test_instructions_mark_broadcast_text_untrusted_and_identity():
    text = server.INSTRUCTIONS
    assert "third-party broadcast speech" in text and "never" in text and "instructions" in text
    assert "single SnapStream user mapped to this API client ID" in text
    assert "api/read scope does not stop writes" in text


@pytest.mark.parametrize("service", ["Configuration", "Config", "config"])
def test_call_read_uses_gateway_path(rt, api, service):
    api.routes[("Config", "GetApiLevel")] = {"GetApiLevelResult": "8.1"}
    out = run(server.ss_call_read(service, "GetApiLevel", {"apiType": 0}))
    assert out["method"] == "Configuration/GetApiLevel" and out["result"] == "8.1"
    assert api.paths() == ["Config/GetApiLevel"]


def test_catalog_and_describe_accept_aliases(rt):
    assert server.ss_api_catalog(service="Notifier", query="alerts")["count"] > 0
    assert "error" in server.ss_api_catalog(service="NoSuchService")
    overview = server.ss_api_catalog()
    assert {s["service"]: s["url_path"] for s in overview["services"]}["Configuration"] == "Config"
    d = server.ss_api_describe("Config", "GetApiLevel")
    assert d["service"] == "Configuration" and d["url_path"] == "Config"
    assert d["param_schemas"]["apiType"]["kind"] == "enum"


def test_describe_returns_clip_properties_schema(rt):
    d = server.ss_api_describe("TaskManager", "AiClipDesigner")
    cp = d["param_schemas"]["clipProperties"]
    assert cp["kind"] == "object" and cp["field_names_are_case_sensitive"] is True
    assert cp["fields"]["Subtitles"]["type"] == "bool"
    assert cp["fields"]["Orientation"]["values"] == {"Vertical": 0, "Horizontal": 1, "Square": 2}
    assert cp["fields"]["Layout"]["values"]["Split"] == 3
    assert cp["fields"]["StartPosition"]["type"] == "long" and "ticks" in cp["fields"]["StartPosition"]["description"]
    assert "SourcePathId" in cp["required"]
    assert d["ui_name"] == "Create for Social"
    d2 = server.ss_api_describe("TaskManager", "ClipFile")
    assert "authTicket" in d2["auth_ticket"] and "{}" in d2["auth_ticket"]


# ---------------------------------------------------------- permission guard
def _b64(text):
    return base64.b64encode(text.encode()).decode()


@pytest.mark.parametrize("call", [
    lambda: server.ss_get_transcript(GUID),
    lambda: server.ss_get_smart_chapters(GUID),
    lambda: server.ss_call_read("Library", "GetTranscript", {"itemId": GUID}),
    lambda: server.ss_call_read("library", "getsmartchapters", {"itemId": GUID}),
], ids=["transcript", "chapters", "call_read_transcript", "call_read_chapters"])
@pytest.mark.parametrize("media_route", [
    {"GetMediaItemResult": None},
    httpx.Response(200, content=b""),
    httpx.Response(200, content=b"", headers={"CustomError": _b64("Access denied")}),
    httpx.Response(500, text="boom"),
], ids=["null", "empty", "fault", "http500"])
def test_permission_guard_refuses_unseen_items(rt, api, call, media_route):
    api.routes[("Library", "GetMediaItem")] = media_route
    out = run(call())
    assert out["kind"] == "not_found_or_no_access", out
    assert all(p == "Library/GetMediaItem" for p in api.paths())  # the guarded method was never called


def test_permission_guard_allows_visible_items(rt, api):
    api.routes[("Library", "GetSmartChapters")] = {"GetSmartChaptersResult": [
        {"Start": 0, "End": 6_000_000_000}, {"Start": 6_000_000_000, "End": 18_123_000_000}]}
    out = run(server.ss_get_smart_chapters(GUID))
    assert api.paths() == ["Library/GetMediaItem", "Library/GetSmartChapters"]
    assert out["result"][1] == {"Start": 6_000_000_000, "Start_seconds": 600.0, "End": 18_123_000_000, "End_seconds": 1812.3}
    run(server.ss_get_transcript(GUID))
    assert api.paths()[-2:] == ["Library/GetMediaItem", "Library/GetTranscript"]


def test_smart_chapters_docstring_says_commercial_breaks():
    doc = server.ss_get_smart_chapters.__doc__ or ""
    assert "commercial" in doc and "NOT AI topic chapters" in doc and "100 ns ticks" in doc


def test_tick_fields_gain_seconds_in_any_read(rt, api):
    api.routes[("Library", "GetBookmarkById")] = {"GetBookmarkByIdResult": {
        "Id": GUID, "TimeCodedTags": [{"Name": "a", "StartTicks": 25_000_000, "EndTicks": 50_000_000}], "Start": 5}}
    out = run(server.ss_get_bookmark(GUID))
    tag = out["result"]["TimeCodedTags"][0]
    assert tag["StartTicks_seconds"] == 2.5 and tag["EndTicks_seconds"] == 5.0
    assert "Start_seconds" not in out["result"]  # plain Start is only ticks for smart chapters


# ---------------------------------------------------------------- ss_status
def test_status_reports_usage_and_rate(rt, api):
    out = run(server.ss_status(check_token=False))
    assert out["usage"]["calls_today"] == 0 and out["usage"]["budget"] == 20000
    assert out["usage"]["rate_per_sec"] == 8.0 and out["usage"]["burst"] == 16
    assert out["limits"]["timeout_seconds"] == 28.0 and out["limits"]["daily_budget"] == 20000
    assert out["limits"]["empty_auth_ticket"] is True
    run(server.ss_whoami())
    run(server.ss_whoami())
    assert run(server.ss_status(check_token=False))["usage"]["calls_today"] == 2


def test_status_usage_without_config(api):
    out = run(server.ss_status())
    assert out["usage"]["calls_today"] == 0 and api.requests == []


# ---------------------------------------------------------------- ss_stage_clip
def test_stage_clip_builds_clipfile_payload(rt, api):
    out = run(server.ss_stage_clip(ITEM_ID, "00:01:02", 75.5, "Budget vote", reason="clip it"))
    assert out["status"] == "staged", out
    assert api.paths() == ["Library/GetMediaItem"]  # read only; nothing was written
    assert out["method"] == "TaskManager/ClipFile"
    assert out["clip"] == {"kind": "clip", "method": "TaskManager/ClipFile", "start_seconds": 62.0,
                           "end_seconds": 75.5, "length_seconds": 13.5}
    sw = rt.stager._staged[out["token"]]
    cp = json.loads(sw.payload_json)["clipProperties"]
    assert cp == {
        "ClipPropertiesType": 1, "ItemId": ITEM_ID, "ChannelId": CHANNEL_ID, "SourcePathId": FOLDER_PATH_ID,
        "SourceFile": "evening-news.mpg", "FolderPathId": FOLDER_PATH_ID, "FolderId": FOLDER_ID,
        "StartPosition": 620_000_000, "EndPosition": 755_000_000, "ClipTitle": "Budget vote",
        "DestinationFolderId": FOLDER_ID,
    }
    p = out["preview"]
    assert "Clip in:     00:01:02" in p and "Clip out:    00:01:15.500" in p and "Clip length: 00:00:13.500" in p
    assert "mapped to this client ID" in p and "private to that user" in p
    assert "Orientation" not in p


def test_stage_clip_commit_sends_stub_and_ticks(rt, api):
    out = run(server.ss_stage_clip(ITEM_ID, "ms:62000", "ms:75500", "Clip", destinationFolderId=GUID))
    assert out["status"] == "staged", out
    api.routes[("TaskManager", "ClipFile")] = {"ClipFileResult": GUID}
    res = run(server.ss_commit_write(out["token"], None))
    assert res["status"] == "committed"
    body = api.bodies()[-1]
    assert body["authTicket"] == {}
    assert body["clipProperties"]["StartPosition"] == 620_000_000 and body["clipProperties"]["DestinationFolderId"] == GUID


def test_stage_clip_social_requires_orientation(rt, api):
    out = run(server.ss_stage_clip(ITEM_ID, 10, 40, "Vertical cut", kind="social"))
    assert out["kind"] == "invalid_argument" and "orientation" in out["error"]
    assert "spec says 0 = vertical, a live test says 1 = vertical" in out["error"]
    assert api.requests == []
    bad = run(server.ss_stage_clip(ITEM_ID, 10, 40, "x", kind="social", orientation=7))
    assert bad["kind"] == "invalid_argument"


def test_stage_clip_social_payload_and_warning(rt, api):
    out = run(server.ss_stage_clip(ITEM_ID, 10, 40, "Vertical cut", kind="Social", orientation=1, layout="split"))
    assert out["status"] == "staged", out
    assert out["method"] == "TaskManager/AiClipDesigner"
    cp = json.loads(rt.stager._staged[out["token"]].payload_json)["clipProperties"]
    assert cp["Subtitles"] is True and cp["Orientation"] == 1 and cp["Layout"] == 3
    assert "subtitles" not in cp
    assert "Orientation value unverified: spec says 0 = vertical, a live test says 1 = vertical" in out["preview"]
    assert "(web app: Create for Social)" in out["preview"]
    no_subs = run(server.ss_stage_clip(ITEM_ID, 10, 40, "x", kind="social", orientation=0, subtitles=False, layout=2))
    cp2 = json.loads(rt.stager._staged[no_subs["token"]].payload_json)["clipProperties"]
    assert cp2["Subtitles"] is False and cp2["Layout"] == 2 and cp2["Orientation"] == 0


@pytest.mark.parametrize("start,end,needle", [
    ("abc", 10, "Invalid clip time"),
    (10, 10, "after start"),
    (10, 10.5, "at least 1 s"),
    ("01:61:00", "01:62:00", "under 60"),
    (-5, 10, "zero or positive"),
])
def test_stage_clip_rejects_bad_times_without_calling(rt, api, start, end, needle):
    out = run(server.ss_stage_clip(ITEM_ID, start, end, "x"))
    assert needle in out["error"] and api.requests == []


def test_stage_clip_bounds_and_access(rt, api):
    out = run(server.ss_stage_clip(ITEM_ID, "00:59:00", "01:00:01", "x"))
    assert "past the end of the recording" in out["error"] and "01:00:00" in out["error"]
    api.routes[("Library", "GetMediaItem")] = {"GetMediaItemResult": None}
    out = run(server.ss_stage_clip(ITEM_ID, 1, 5, "x"))
    assert out["kind"] == "not_found_or_no_access"
    assert rt.stager.pending()["count"] == 0
    assert run(server.ss_stage_clip("not-a-guid", 1, 5, "x"))["kind"] == "invalid_argument"
    assert run(server.ss_stage_clip(ITEM_ID, 1, 5, "x", kind="gif"))["kind"] == "invalid_argument"


@pytest.mark.parametrize("service", ["Config", "Configuration"])
def test_stage_write_accepts_gateway_path_alias(rt, api, service):
    staged = server.ss_stage_write(service, "DeleteProfile", {"id": GUID}, reason="alias test")
    assert staged["status"] == "staged" and staged["method"] == "Configuration/DeleteProfile"
    out = run(server.ss_commit_write(staged["token"], None))
    assert out["status"] == "committed"
    assert api.paths() == ["Config/DeleteProfile"]


# ------------------------------------- 1 s minimum is checked in ticks (api-3)
@pytest.mark.parametrize("start,end", [(0.4, 1.4), (0.9, 1.9), (1.3, 2.3), (1.8, 2.8), (3.1, 4.1)])
def test_stage_clip_exact_one_second_is_accepted(rt, api, start, end):
    out = run(server.ss_stage_clip(ITEM_ID, start, end, "One second"))
    assert out["status"] == "staged", out
    cp = json.loads(rt.stager._staged[out["token"]].payload_json)["clipProperties"]
    assert cp["EndPosition"] - cp["StartPosition"] == 10_000_000


def test_stage_clip_just_under_one_second_is_refused(rt, api):
    out = run(server.ss_stage_clip(ITEM_ID, 0.4, 1.3999999, "x"))
    assert "at least 1 s" in out["error"] and api.requests == []
