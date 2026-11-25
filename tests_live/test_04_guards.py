"""PYTEST_DONT_REWRITE

Read tier 5: negative and guard tests. Nothing here commits a write: the write firewall refuses
any write in this tier, and the module fixture checks that nothing is left pending at the end.
"Refused locally" is checked against the session call log, so it proves no API call was made."""

from __future__ import annotations

import time
import uuid

import pytest

from snapstream_mcp import server
from tests_live.helpers import result_of


def _calls(live, mark):
    return [k for k, _w in live.calls_since(mark)]


def test_unknown_param_refused_locally(live, media_item):
    mark = len(live.call_log)
    out = live.run(server.ss_call_read("Library", "GetMediaItem", {"itemId": media_item["Id"], "bogusParam": 1}))
    assert "error" in out and "valid_params" in out, "unknown parameter was not refused with valid_params"
    assert any(p.startswith("itemId") for p in out["valid_params"]), "valid_params does not list itemId"
    assert _calls(live, mark) == [], "an API call was made for a locally invalid request"


def test_write_method_refused_by_call_read(live):
    mark = len(live.call_log)
    out = live.run(server.ss_call_read("TaskManager", "ClipFile", {}))
    assert out.get("access") == "write" and "ss_stage_write" in out.get("error", ""), "ClipFile was not refused as a write"
    assert _calls(live, mark) == [], "an API call was made for a write method via ss_call_read"


def test_blocked_method_refused_for_read_and_stage(live):
    blocked = [m for m in live.rt.catalog.all() if m.access == "blocked"]
    assert blocked, "catalog has no blocked methods"
    mark = len(live.call_log)
    for info in (blocked[0], live.rt.catalog.get("AuthenticationManager", "Login")):
        if info is None:
            continue
        out = live.run(server.ss_call_read(info.service, info.name, {}))
        assert out.get("access") == "blocked", f"{info.key} was not refused as blocked by ss_call_read"
        st = server.ss_stage_write(info.service, info.name, {})
        assert st.get("status") == "refused", f"{info.key} was not refused by ss_stage_write"
    assert _calls(live, mark) == [], "an API call was made for a blocked method"
    assert server.ss_pending_writes()["count"] == 0, "a blocked method left a pending write"


def test_random_guid_transcript_not_found(live):
    mark = len(live.call_log)
    out = live.run(server.ss_get_transcript(str(uuid.uuid4())))
    assert out.get("kind") == "not_found_or_no_access", f"random GUID transcript: kind={out.get('kind')}"
    calls = _calls(live, mark)
    assert "Library/GetTranscript" not in calls, "GetTranscript was called although the permission check failed"
    assert calls == ["Library/GetMediaItem"], f"expected only the GetMediaItem guard call, got {calls}"
    live.assert_no_leaks(out, "ss_get_transcript random GUID")


def test_bad_search_type_refused_without_a_call(live):
    mark = len(live.call_log)
    for tool in (server.ss_search_recordings, server.ss_count_recording_matches, server.ss_search_guide):
        out = live.run(tool("news", type="bogus_type"))
        assert out.get("kind") == "invalid_argument", f"{tool.__name__}: bad type not refused"
    assert _calls(live, mark) == [], "an API call was made for a bad search type"


def _clip_props(media_item, **extra):
    props = {
        "ClipPropertiesType": 1,
        "ItemId": media_item["Id"],
        "SourcePathId": media_item["FolderPathId"],
        "SourceFile": media_item["Filename"],
        "StartPosition": 60 * 10_000_000,
        "EndPosition": 70 * 10_000_000,
        "ClipTitle": "MCP live guard test (never committed)",
        "DestinationFolderId": media_item["FolderId"],
    }
    props.update(extra)
    return props


def test_unknown_nested_key_refused_at_stage(live, media_item):
    mark = len(live.call_log)
    out = server.ss_stage_write("TaskManager", "ClipFile", {"clipProperties": _clip_props(media_item, BogusField=1)})
    assert out.get("status") == "refused" and "unknown field" in out.get("error", ""), "unknown nested key was not refused"
    assert _calls(live, mark) == [] and server.ss_pending_writes()["count"] == 0, "refused stage made a call or left a pending write"


def test_case_only_mismatch_refused_at_stage(live, media_item):
    props = _clip_props(media_item)
    props["cliptitle"] = props.pop("ClipTitle")
    out = server.ss_stage_write("TaskManager", "ClipFile", {"clipProperties": props})
    assert out.get("status") == "refused" and "did you mean ClipTitle" in out.get("error", ""), "case-only mismatch was not refused"
    out = server.ss_stage_write("TaskManager", "AiClipDesigner", {"clipProperties": _clip_props(media_item, subtitles=True)})
    assert out.get("status") == "refused" and "did you mean Subtitles" in out.get("error", ""), "subtitles vs Subtitles was not refused"
    assert server.ss_pending_writes()["count"] == 0, "a refused stage left a pending write"


def test_millisecond_positions_refused(live, media_item):
    # Raw ClipProperties with milliseconds where ticks belong: a 10 ms span, refused with a hint.
    out = server.ss_stage_write(
        "TaskManager", "ClipFile", {"clipProperties": _clip_props(media_item, StartPosition=60_000, EndPosition=70_000)}
    )
    assert out.get("status") == "refused" and "milliseconds" in out.get("error", ""), "ms-sized tick positions were not refused"
    # ss_stage_clip given millisecond numbers as seconds: past the end of the recording.
    out = live.run(server.ss_stage_clip(media_item["Id"], 60_000, 70_000, "MCP live guard test (never committed)"))
    assert "error" in out and out.get("kind") == "invalid_argument", "ss_stage_clip accepted ms-sized positions"
    assert server.ss_pending_writes()["count"] == 0, "a refused stage left a pending write"


def test_stage_then_cancel(live, media_item):
    out = live.run(server.ss_stage_clip(media_item["Id"], 60, 70, "MCP live guard test (never committed)", reason="live guard test; canceled"))
    assert out.get("status") == "staged", f"ss_stage_clip did not stage (status={out.get('status')}, kind={out.get('kind')})"
    token = out["token"]
    pend = server.ss_pending_writes()
    assert pend["count"] == 1 and pend["pending"][0]["stage_id"] == out["stage_id"], "staged write not listed as pending"
    c = server.ss_cancel_staged(token)
    assert c.get("status") == "canceled", "ss_cancel_staged did not cancel"
    assert server.ss_pending_writes()["count"] == 0, "pending writes not empty after cancel"


def test_reused_and_expired_tokens_refused(live, media_item):
    mark = len(live.call_log)
    # Reused: a canceled token cannot be committed.
    out = live.run(server.ss_stage_clip(media_item["Id"], 60, 70, "MCP live guard test (never committed)", reason="live guard test; canceled"))
    assert out.get("status") == "staged", "ss_stage_clip did not stage"
    server.ss_cancel_staged(out["token"])
    res = live.run(server.ss_commit_write(out["token"], None))
    assert res.get("status") == "refused", "a canceled token was not refused"

    # Expired: shift the stager clock past the 10 minute TTL, then commit.
    out = live.run(server.ss_stage_clip(media_item["Id"], 60, 70, "MCP live guard test (never committed)", reason="live guard test; expires"))
    assert out.get("status") == "staged", "ss_stage_clip did not stage"
    stager = live.rt.stager
    real_clock = stager._clock
    stager._clock = lambda: real_clock() + stager.ttl + 60
    try:
        assert stager._staged[out["token"]].expires <= stager._clock(), "test setup: the staged write is not expired"
        res = live.run(server.ss_commit_write(out["token"], None))
    finally:
        stager._clock = real_clock
    assert res.get("status") == "refused" and "expired" in res.get("error", ""), "an expired token was not refused"
    res = live.run(server.ss_commit_write(out["token"], None))
    assert res.get("status") == "refused", "an expired token was accepted on the second try"
    writes = [k for k, w in live.calls_since(mark) if w]
    assert writes == [], f"a write call reached the client: {writes}"
    assert server.ss_pending_writes()["count"] == 0, "pending writes not empty"
