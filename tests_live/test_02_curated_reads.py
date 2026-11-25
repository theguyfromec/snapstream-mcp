"""PYTEST_DONT_REWRITE

Read tier 2 and 3: every curated read tool with real ids, checking response shape (ISO dates, no
/Date(, _seconds siblings, no error key, no secrets), paging, transcript windowing and search
types. Values are compared in memory only; messages never quote them."""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any, Dict, List

import pytest

from snapstream_mcp import server
from snapstream_mcp.server import GUIDE_SEARCH_TYPES, RECORDING_SEARCH_TYPES
from tests_live.helpers import as_list, first_id, result_of, usable_guid

LINE_TS = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\] ")
TITLE_WORDS = ("news", "today", "morning", "live")
CAPTION_WORDS = ("president", "news", "weather", "today")


def ok(live, out: Dict[str, Any], label: str, method: str = "") -> Dict[str, Any]:
    live.assert_ok(out, label)
    live.assert_shape(out, label, method)
    return out


def _clock(line: str) -> float:
    m = LINE_TS.match(line)
    assert m, "transcript line without an [hh:mm:ss] timestamp"
    h, mi, s = (int(x) for x in m.groups())
    return h * 3600 + mi * 60 + s


def _iso(s: str) -> _dt.datetime:
    return _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---------------------------------------------------------------------- Library
def test_list_media_items_paging(live):
    p1 = ok(live, live.run(server.ss_list_media_items(index=0, count=4)), "ss_list_media_items page 1")
    items1 = as_list(result_of(p1))
    assert p1["params_sent"]["count"] == 4 and p1["params_sent"]["index"] == 0, "paging params not sent as given"
    assert len(items1) <= 4, f"count=4 returned {len(items1)} items"
    if len(items1) < 4:
        pytest.skip("library has fewer than 4 media items; offset paging cannot be checked")
    ids1 = [it.get("Id") for it in items1]
    for attempt in range(2):  # new recordings can shift the first page between the two calls
        p2 = ok(live, live.run(server.ss_list_media_items(index=2, count=2)), "ss_list_media_items page 2")
        ids2 = [it.get("Id") for it in as_list(result_of(p2))]
        assert len(ids2) <= 2, f"count=2 returned {len(ids2)} items"
        if ids2 and ids2[0] in ids1[1:]:
            break
        p1 = live.run(server.ss_list_media_items(index=0, count=4))
        ids1 = [it.get("Id") for it in as_list(result_of(p1))]
    else:
        pytest.fail("index=2 did not continue from the first page (checked twice)")


def test_full_media_items_have_shape(live):
    out = ok(live, live.run(server.ss_list_media_items(index=0, count=3, brief=False)), "ss_list_media_items brief=False")
    for it in as_list(result_of(out)):
        assert usable_guid(it.get("Id")), "media item without a GUID Id"


def test_count_cap_clamps(live):
    hi = ok(live, live.run(server.ss_list_channels(count=5000)), "ss_list_channels count=5000")
    assert hi["params_sent"]["count"] == 1000, f"count 5000 was sent as {hi['params_sent']['count']}, not clamped to 1000"
    assert len(as_list(result_of(hi))) <= 1000, "more than 1000 channels came back"
    lo = ok(live, live.run(server.ss_list_channels(count=0)), "ss_list_channels count=0")
    assert lo["params_sent"]["count"] == 1, f"count 0 was sent as {lo['params_sent']['count']}, not clamped to 1"
    assert len(as_list(result_of(lo))) <= 1, "count=1 returned more than one channel"


def test_get_media_item(live, media_item):
    out = ok(live, live.run(server.ss_get_media_item(media_item["Id"])), "ss_get_media_item")
    item = result_of(out)
    assert isinstance(item, dict) and item.get("Id", "").lower() == media_item["Id"].lower(), "GetMediaItem returned another item"
    assert isinstance(item.get("Duration_seconds"), (int, float)), "Duration has no Duration_seconds"


def test_transcript_paging_by_caption(live, media_item):
    iid = media_item["Id"]
    first = live.assert_ok(live.run(server.ss_get_transcript(iid, max_chars=500)), "ss_get_transcript window 1")
    assert first.get("caption_count", 0) > 0, "transcript has no captions"
    assert first.get("time_basis") == "offset within the recording", "caption StartTime was not read as an offset"
    if not first.get("truncated"):
        pytest.skip("transcript fits in one 500-character window")
    seen = [first["transcript"].splitlines()[0]]
    nxt = first["next_start_caption"]
    for page in range(2, 5):
        win = live.assert_ok(
            live.run(server.ss_get_transcript(iid, start_caption=nxt, max_chars=500)), f"ss_get_transcript window {page}"
        )
        lines = win["transcript"].splitlines()
        assert lines, f"window {page} is empty although the previous one was truncated"
        assert win["returned_lines"] == len(lines), f"window {page}: returned_lines does not match the lines"
        assert len(win["transcript"]) <= 500 + max(len(x) for x in lines), f"window {page} exceeds max_chars"
        seen.append(lines[0])
        if not win.get("truncated"):
            break
        assert win["next_start_caption"] > nxt, f"window {page}: next_start_caption did not advance"
        nxt = win["next_start_caption"]
    assert len(set(seen)) == len(seen), "two windows started with the same line"


def test_transcript_offset(live, media_item):
    dur = media_item.get("Duration_seconds") or 0
    if dur < 90:
        pytest.skip("recording shorter than 90 s")
    out = live.assert_ok(live.run(server.ss_get_transcript(media_item["Id"], offset_seconds=60, max_chars=1000)), "ss_get_transcript offset 60")
    lines = out["transcript"].splitlines()
    if not lines:
        pytest.skip("no captions after 60 s")
    assert _clock(lines[0]) >= 60, "offset_seconds=60 returned a caption before 00:01:00"
    assert all(_clock(x) >= 60 for x in lines), "a caption before the offset came back"


def test_transcript_text_filter(live, media_item):
    iid = media_item["Id"]
    base = live.assert_ok(live.run(server.ss_get_transcript(iid, max_chars=3000)), "ss_get_transcript")
    words = sorted({w.lower() for w in re.findall(r"[A-Za-z]{6,}", base["transcript"])}, key=len, reverse=True)
    if not words:
        pytest.skip("no word of 6+ letters in the first transcript window")
    needle = words[0]
    out = live.assert_ok(live.run(server.ss_get_transcript(iid, contains=needle, max_chars=5000)), "ss_get_transcript contains")
    lines = out["transcript"].splitlines()
    assert lines, "text filter on a word from the transcript returned nothing"
    assert all(needle in x.lower() for x in lines), "text filter returned a line without the word"
    assert out["returned_lines"] == len(lines), "returned_lines does not match the filtered lines"


def test_smart_chapters(live, media_item):
    out = ok(live, live.run(server.ss_get_smart_chapters(media_item["Id"])), "ss_get_smart_chapters", "GetSmartChapters")
    for ch in as_list(result_of(out)):
        if isinstance(ch, dict) and isinstance(ch.get("Start"), int):
            assert "Start_seconds" in ch, "smart chapter Start has no Start_seconds"


def test_search_in_recording(live, media_item):
    out = ok(live, live.run(server.ss_search_in_recording("the", media_item["Id"], count=5)), "ss_search_in_recording")
    assert out["params_sent"]["mediaItem"] == media_item["Id"].lower(), "mediaItem was not sent"


def test_library_channels(live):
    out = ok(live, live.run(server.ss_list_library_channels()), "ss_list_library_channels")
    assert first_id(as_list(result_of(out))), "no channel ids in Library/GetAllPastAndCurrentChannels"


def test_clips_and_bookmark(live, media_item, bookmark_id):
    ok(live, live.run(server.ss_list_clips_and_bookmarks(media_item["Id"], count=5)), "ss_list_clips_and_bookmarks")
    out = ok(live, live.run(server.ss_get_bookmark(bookmark_id)), "ss_get_bookmark")
    assert isinstance(result_of(out), dict), "GetBookmarkById returned no object"


def test_storage_status(live):
    out = ok(live, live.run(server.ss_storage_status()), "ss_storage_status")
    assert as_list(result_of(out)), "Library/GetDrives returned no drives"


# ----------------------------------------------------------------------- Search
@pytest.mark.parametrize("name", sorted(RECORDING_SEARCH_TYPES))
def test_recording_search_type_named(live, name):
    out = ok(live, live.run(server.ss_count_recording_matches("news", type=name)), f"ss_count_recording_matches type={name}")
    assert out["params_sent"]["type"] == RECORDING_SEARCH_TYPES[name], f"type {name} sent as the wrong flag value"


@pytest.mark.parametrize("name", sorted(GUIDE_SEARCH_TYPES))
def test_guide_search_type_named(live, name):
    out = ok(live, live.run(server.ss_search_guide("news", count=1, type=name)), f"ss_search_guide type={name}")
    assert out["params_sent"]["type"] == GUIDE_SEARCH_TYPES[name], f"guide type {name} sent as the wrong flag value"


def _first_hits(live, type_name: str, words) -> List[Dict[str, Any]]:
    for w in words:
        out = ok(live, live.run(server.ss_search_recordings(w, count=20, type=type_name)), f"ss_search_recordings type={type_name}")
        res = result_of(out)
        hits = [r for r in as_list(res.get("Results") if isinstance(res, dict) else None) if isinstance(r, dict)]
        if hits:
            return hits
    return []


def test_caption_and_title_search_shapes_differ(live):
    cap = _first_hits(live, "captions", CAPTION_WORDS)
    title = _first_hits(live, "title", TITLE_WORDS)
    if not cap or not title:
        pytest.skip("no caption or no title hits for the common words tried")
    assert any("End_seconds" in r or "Start_seconds" in r for r in cap), "caption hits carry no offset into the recording"
    ids = [r.get("ItemId") or r.get("Id") for r in title]
    assert len(set(ids)) == len(ids), "title search returned more than one hit for one recording"


def test_search_type_list_is_ored(live):
    as_list_form = ok(live, live.run(server.ss_count_recording_matches("news", type=["title", "captions"])), "list type")
    as_pipe_form = ok(live, live.run(server.ss_count_recording_matches("news", type="title|captions")), "pipe type")
    assert as_list_form["params_sent"]["type"] == 6, "['title', 'captions'] was not OR-ed to 6"
    assert as_pipe_form["params_sent"]["type"] == 6, "'title|captions' was not OR-ed to 6"
    n = result_of(as_list_form)
    n = n.get("NumberOfResults") if isinstance(n, dict) else n
    assert isinstance(n, int), "SearchRecordingsCount returned no count"


# -------------------------------------------------------------------- Scheduler
def test_guide_for_channel(live, channel_id):
    out = ok(live, live.run(server.ss_guide_for_channel(channel_id, count=20)), "ss_guide_for_channel")
    for a in as_list(result_of(out)):
        if usable_guid(a.get("ChannelId")):
            assert a["ChannelId"].lower() == channel_id.lower(), "an airing from another channel came back"


def test_guide_by_time(live):
    begin = _dt.datetime.now(_dt.timezone.utc)
    out = ok(live, live.run(server.ss_guide_by_time(count=20)), "ss_guide_by_time")
    slack = _dt.timedelta(minutes=5)
    for a in as_list(result_of(out)):
        if isinstance(a.get("StartTime"), str):
            st = _iso(a["StartTime"])
            assert begin - slack <= st <= begin + _dt.timedelta(hours=2) + slack, "an airing starts outside the 2 h window"


def test_whats_on_and_airing(live, channel_id, airing):
    ok(live, live.run(server.ss_whats_on([channel_id])), "ss_whats_on")
    out = ok(live, live.run(server.ss_get_airing(airing["Id"])), "ss_get_airing")
    assert isinstance(result_of(out), dict), "GetAiring returned no object"


@pytest.mark.parametrize("tool", ["ss_list_scheduled_recordings", "ss_list_current_recordings", "ss_list_recent_recordings"])
def test_recording_lists(live, tool):
    out = ok(live, live.run(getattr(server, tool)(count=10)), tool)
    assert len(as_list(result_of(out))) <= 10, f"{tool}: count=10 returned more items"


def test_recording_jobs_and_detail(live, job_id):
    ok(live, live.run(server.ss_list_recording_jobs(count=5)), "ss_list_recording_jobs")
    out = ok(live, live.run(server.ss_call_read("Scheduler", "GetRecordingJob", {"id": job_id})), "Scheduler/GetRecordingJob")
    assert isinstance(result_of(out), dict), "GetRecordingJob returned no object"


def test_scheduler_status(live):
    out = live.run(server.ss_scheduler_status())
    live.assert_no_leaks(out, "ss_scheduler_status")
    for part in ("GetStats", "GetUpdateStatus", "GetLastRebuild"):
        assert part in out, f"ss_scheduler_status has no {part}"
        v = out[part]
        assert not (isinstance(v, dict) and "error" in v), f"ss_scheduler_status {part} failed"


# ------------------------------------------------------------------ TaskManager
def test_get_job(live, task_list_id):
    out = ok(live, live.run(server.ss_get_job(task_list_id)), "ss_get_job")
    assert isinstance(result_of(out), dict), "GetTaskList returned no object"
