"""Offline checks of the live suite's helpers (tests_live/helpers.py and the write tier's lookup
helpers): the catalog sweep param builder, the write firewall, outcome classification, the leak
and shape scanners, failure-output hardening, discovery (with the server tools mocked) and the
clip lookup and delete guards. No network."""

from __future__ import annotations

import asyncio
import types
import uuid

import pytest

from snapstream_mcp.catalog import coerce_params
from snapstream_mcp.client import SnapStreamError
from snapstream_mcp import server
from tests_live import test_10_writes as writes
from tests_live.helpers import (
    Discovery, WriteFirewall, build_sweep_params, classify, harden_failure_options, leak_labels, shape_issues,
    strip_frame_values,
)

IDS = {k: str(uuid.uuid4()) for k in (
    "media_item", "channel", "airing", "job", "task_list", "bookmark", "folder", "folder_path", "drive",
    "machine", "series", "episode", "movie", "recording", "context",
)}


def test_sweep_params_are_valid_for_every_read_only_read(catalog):
    reads = [m for m in catalog.all() if m.access == "read" and m.read_only is True]
    assert len(reads) > 200
    for info in reads:
        params, _synthetic = build_sweep_params(info, IDS, catalog)
        coerce_params(info, params)  # raises on a name or type the API would not take
        if "count" in params:
            assert params["count"] == 1 and params.get("index", 0) == 0


def test_sweep_params_map_ids_by_name_and_method(catalog):
    p, syn = build_sweep_params(catalog.get("Scheduler", "GetAiring"), IDS, catalog)
    assert p == {"id": IDS["airing"]} and syn == []
    p, _ = build_sweep_params(catalog.get("Scheduler", "GetRecordingJob"), IDS, catalog)
    assert p == {"id": IDS["job"]}
    p, _ = build_sweep_params(catalog.get("Library", "GetTranscript"), IDS, catalog)
    assert p == {"itemId": IDS["media_item"]}
    p, syn = build_sweep_params(catalog.get("Library", "GetTranscript"), {}, catalog)
    assert syn == ["itemId"] and p["itemId"] not in IDS.values()
    p, _ = build_sweep_params(catalog.get("Scheduler", "GetAiringsByStartTimeRange"), IDS, catalog)
    assert p["startBegin"] < p["startEnd"]


def test_firewall_caps_writes():
    fw = WriteFirewall(max_creates=2, writes_allowed=True)
    with pytest.raises(SnapStreamError):
        fw.authorize("TaskManager/ClipFile", {})  # not enabled
    fw.enabled = True
    fw.authorize("TaskManager/ClipFile", {})
    fw.authorize("TaskManager/AiClipDesigner", {})
    with pytest.raises(SnapStreamError):
        fw.authorize("TaskManager/ClipFile", {})  # cap of 2
    item = str(uuid.uuid4())
    with pytest.raises(SnapStreamError):
        fw.authorize("Library/DeleteMediaItem", {"itemId": item})  # not registered
    fw.allow_delete(item.upper())
    fw.authorize("Library/DeleteMediaItem", {"itemId": item})
    with pytest.raises(SnapStreamError):
        fw.authorize("Library/DeleteMediaItem", {"itemId": item})  # only once
    with pytest.raises(SnapStreamError):
        fw.authorize("Scheduler/RecordAiring", {})  # not on the allowlist
    assert len(fw.violations) == 5


def test_classify():
    assert classify({"result": 1})[0] == "ok"
    assert classify({"error": "x", "kind": "api_fault", "http_status": 200})[:2] == ("api_fault", 200)
    assert classify({"error": "x", "kind": "http", "http_status": 404})[0] == "http_404"
    assert classify({"error": "x", "kind": "http", "http_status": 500})[0] == "http"
    assert classify({"error": "x", "kind": "timeout"})[0] == "transport"
    assert classify({"error": "Unexpected error: KeyError: 'a'"})[0] == "local_crash"
    assert classify({"error": "Invalid parameters", "valid_params": []})[0] == "local_refusal"


def test_leak_and_shape_scanners():
    assert leak_labels('{"a": "[REDACTED]", "b": "Bearer [REDACTED]"}', []) == []
    assert leak_labels("x eyJabcdefghij.klmnopqrstuv.wxyz", []) == ["JWT-shaped string"]
    assert leak_labels("contains s3cr3t-value", ["s3cr3t-value"])[0].startswith("configured secret")
    assert leak_labels("https://x/y?X-Amz-Signature=abc123", []) == ["credential URL query parameter"]
    issues = shape_issues({"a": "/Date(123)/", "StartPosition": 10, "Duration": "PT5S", "b": [{"T": "2026-13-01T00:00Z"}]},
                          {"StartPosition"})
    assert len(issues) == 4
    assert shape_issues({"StartPosition": 10, "StartPosition_seconds": 0.0, "Duration": "PT5S", "Duration_seconds": 5.0},
                        {"StartPosition"}) == []


def test_firewall_refuses_writes_without_the_option():
    fw = WriteFirewall(max_creates=2)  # writes_allowed defaults to False
    fw.enabled = True  # what the write tier's fixture does
    with pytest.raises(SnapStreamError, match="--live-writes"):
        fw.authorize("TaskManager/ClipFile", {})
    item = str(uuid.uuid4())
    fw.allow_delete(item)
    with pytest.raises(SnapStreamError, match="--live-writes"):
        fw.authorize("Library/DeleteMediaItem", {"itemId": item})
    assert fw.creates == 0 and fw.deletes == 0 and len(fw.violations) == 2


# ------------------------------------------------------------ failure output
def test_harden_failure_options():
    for style, want in (("auto", "short"), ("long", "short"), ("short", "short"), ("line", "line"),
                        ("no", "no"), ("native", "native")):
        opt = types.SimpleNamespace(tbstyle=style, showlocals=True, fulltrace=True)
        harden_failure_options(opt)
        assert (opt.tbstyle, opt.showlocals, opt.fulltrace) == (want, False, False)


def _helper(out, label):
    # A plain raise: this file is assertion-rewritten, the live suite is not (PYTEST_DONT_REWRITE).
    if "error" in out:
        raise AssertionError(f"{label}: failed")


def test_strip_frame_values_removes_args_and_locals():
    secret_title, body = "SECRET_" + "TITLE_X", "RESPONSE_" + "BODY_Y"  # built so the source lines do not hold them
    media_item = {"Title": secret_title}  # noqa: F841 - a local the repr would print
    with pytest.raises(AssertionError) as excinfo:
        _helper({"error": "boom", "body": body}, "lbl")
    for style in ("long", "short"):
        rep = excinfo.getrepr(funcargs=True, showlocals=True, style=style)
        assert "RESPONSE_BODY_Y" in str(rep)  # the unhardened repr prints it
        assert strip_frame_values(rep) > 0
        text = str(rep)
        assert "RESPONSE_BODY_Y" not in text and "SECRET_TITLE_X" not in text
        assert "lbl: failed" in text
    assert strip_frame_values("plain text") == 0


# ------------------------------------------------------------ discovery (mocked tools)
class _FakeLive:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.run_stamp = "2026-11-25T00:00:00Z"
        self.report = types.SimpleNamespace(cleanup=[], writes=[])

    def run(self, coro, timeout=None):
        return self.loop.run_until_complete(coro)


@pytest.fixture
def fake_live():
    live = _FakeLive()
    yield live
    live.loop.close()


def _media(iid, **kw):
    base = {"Id": iid, "Title": "Show", "FolderPathId": str(uuid.uuid4()), "FolderId": str(uuid.uuid4()),
            "Filename": f"{iid}.mp4", "Duration_seconds": 3600.0}
    base.update(kw)
    return base


def _async(fn):
    async def wrapper(*a, **kw):
        return fn(*a, **kw)
    return wrapper


def test_media_item_falls_back_when_every_hit_is_rejected(monkeypatch, fake_live):
    """Every caption hit is still recording: no check is spent on them, and the Library fallback
    runs and finds the completed item."""
    live_ids = [str(uuid.uuid4()) for _ in range(6)]
    done = str(uuid.uuid4())
    calls = {"get": [], "list": 0}

    def search(query, index=0, count=25, type="default"):
        hits = [{"ItemId": i, "InProgress": True} for i in live_ids]
        return {"result": {"Results": hits if index == 0 else []}}

    def get(iid):
        calls["get"].append(iid)
        return {"result": _media(iid, InProgress=iid in live_ids)}

    def listing(index=0, count=50, brief=True):
        calls["list"] += 1
        return {"result": ([{"Id": i} for i in live_ids] + [{"Id": done}]) if index == 0 else []}

    monkeypatch.setattr(server, "ss_search_recordings", _async(search))
    monkeypatch.setattr(server, "ss_get_media_item", _async(get))
    monkeypatch.setattr(server, "ss_list_media_items", _async(listing))
    monkeypatch.setattr(server, "ss_get_transcript", _async(lambda iid, max_chars=0: {"caption_count": 3}))
    d = Discovery(fake_live)
    item = d.media_item()
    assert item is not None and item["Id"] == done
    assert calls["list"] == 1
    assert calls["get"][-1] == done and calls["get"].count(done) == 1
    assert d.tool_error("media_item") is None


def test_media_item_search_skips_in_progress_hits(monkeypatch, fake_live):
    live_id, good = str(uuid.uuid4()), str(uuid.uuid4())
    seen_get = []
    queries = []

    def search(q, index=0, count=25, type="default"):
        queries.append(q)
        return {"result": {"Results": [{"ItemId": live_id, "InProgress": True}, {"ItemId": good, "Clip": False}]
                           if index == 0 else []}}

    monkeypatch.setattr(server, "ss_search_recordings", _async(search))
    monkeypatch.setattr(server, "ss_get_media_item", _async(lambda iid: (seen_get.append(iid), {"result": _media(iid)})[1]))
    monkeypatch.setattr(server, "ss_get_transcript", _async(lambda iid, max_chars=0: {"caption_count": 1}))
    item = Discovery(fake_live).media_item()
    assert item["Id"] == good and seen_get == [good]
    assert queries == ["news -IS:CLIP"]


def test_media_item_retries_a_word_plain_when_the_filtered_query_finds_nothing(monkeypatch, fake_live):
    good = str(uuid.uuid4())
    queries = []

    def search(q, index=0, count=25, type="default"):
        queries.append(q)
        return {"result": {"Results": [{"ItemId": good}] if ("-IS:CLIP" not in q and index == 0) else []}}

    monkeypatch.setattr(server, "ss_search_recordings", _async(search))
    monkeypatch.setattr(server, "ss_get_media_item", _async(lambda iid: {"result": _media(iid)}))
    monkeypatch.setattr(server, "ss_get_transcript", _async(lambda iid, max_chars=0: {"caption_count": 1}))
    assert Discovery(fake_live).media_item()["Id"] == good
    assert queries == ["news -IS:CLIP", "news"]


def test_media_item_transcript_error_is_a_tool_error(monkeypatch, fake_live):
    iid = str(uuid.uuid4())
    monkeypatch.setattr(server, "ss_search_recordings", _async(lambda q, index=0, count=25, type="default": {
        "result": {"Results": [{"ItemId": iid}] if index == 0 else []}}))
    monkeypatch.setattr(server, "ss_get_media_item", _async(lambda i: {"result": _media(i)}))
    monkeypatch.setattr(server, "ss_list_media_items", _async(lambda index=0, count=50, brief=True: {"result": []}))
    monkeypatch.setattr(server, "ss_get_transcript", _async(lambda i, max_chars=0: {"error": "x", "kind": "api_fault"}))
    d = Discovery(fake_live)
    assert d.media_item() is None
    assert "ss_get_transcript" in d.tool_error("media_item")
    assert "'transcript_error': 1" in d.reasons["media_item"] and "'no_captions': 0" in d.reasons["media_item"]


def test_media_item_genuinely_empty_is_not_a_tool_error(monkeypatch, fake_live):
    monkeypatch.setattr(server, "ss_search_recordings", _async(lambda q, index=0, count=25, type="default": {
        "result": {"Results": []}}))
    monkeypatch.setattr(server, "ss_list_media_items", _async(lambda index=0, count=50, brief=True: {"result": []}))
    d = Discovery(fake_live)
    assert d.media_item() is None and d.tool_error("media_item") is None


def test_finder_tool_error_vs_empty(monkeypatch, fake_live):
    monkeypatch.setattr(server, "ss_list_recording_jobs", _async(lambda count=100: {
        "error": "fault", "kind": "api_fault", "http_status": 200}))
    d = Discovery(fake_live)
    assert d.job_id() is None
    assert "kind=api_fault" in d.tool_error("job_id") and d.reasons["job_id"].startswith("tool error")

    monkeypatch.setattr(server, "ss_list_recording_jobs", _async(lambda count=100: {"result": []}))
    d = Discovery(fake_live)
    assert d.job_id() is None and d.tool_error("job_id") is None
    assert d.reasons["job_id"] == "Scheduler/GetRecordingJobs returned no job"

    monkeypatch.setattr(server, "ss_call_read", _async(lambda s, m, p=None, compact_output=True, max_chars=0: {
        "error": "boom", "kind": "http", "http_status": 500}))
    d = Discovery(fake_live)
    assert d.task_list_id() is None and "http_status=500" in d.tool_error("task_list_id")

    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(server, "ss_storage_status", _async(boom))
    d = Discovery(fake_live)
    assert d.drive() is None and d.tool_error("drive") == "discovery raised RuntimeError"


def test_channel_fallback_clears_tool_error(monkeypatch, fake_live):
    cid = str(uuid.uuid4())
    monkeypatch.setattr(server, "ss_list_channels", _async(lambda count=200: {"error": "x", "kind": "api_fault"}))
    monkeypatch.setattr(server, "ss_list_library_channels", _async(lambda: {"result": [{"Id": cid}]}))
    d = Discovery(fake_live)
    assert d.channel_id() == cid and d.tool_error("channel_id") is None
    monkeypatch.setattr(server, "ss_list_library_channels", _async(lambda: {"result": []}))
    d = Discovery(fake_live)
    assert d.channel_id() is None and "ss_list_channels" in d.tool_error("channel_id")


# ------------------------------------------------------------ write tier lookup and delete guards
def test_paged_follows_truncation(monkeypatch, fake_live):
    data = [{"Id": str(uuid.uuid4())} for _ in range(150)]
    seen = []

    def read(service, method, params=None, compact_output=True, max_chars=0):
        i, c = params["index"], params["count"]
        seen.append(i)
        page = data[i:i + c]
        if len(page) > 66:  # a capped full page, as cap_output returns it
            return {"result": page[:66], "truncated": True, "items_returned": 66}
        return {"result": page, "truncated": False}

    monkeypatch.setattr(server, "ss_call_read", _async(read))
    items, err = writes._paged(fake_live, "Library", "GetMediaItems", {}, page=100, max_items=400)
    assert err is None and [x["Id"] for x in items] == [x["Id"] for x in data]
    assert seen == [0, 66, 132]

    monkeypatch.setattr(server, "ss_call_read", _async(lambda *a, **k: {"result_text": "x", "truncated": True}))
    items, err = writes._paged(fake_live, "Library", "GetMediaItems", {}, page=100, max_items=400)
    assert items == [] and "larger than the output cap" in err


def _write_run(fake_live):
    parent = _media(str(uuid.uuid4()))
    run = writes.WriteRun(fake_live, parent)
    rec = {"title": run.title()}
    run.records.append(rec)
    return run, rec


@pytest.mark.parametrize("extra, why", [
    ({"Clip": False}, "not marked as a clip"),
    ({}, "not marked as a clip"),  # no Clip flag and no ClipParentId
    ({"Clip": True, "ClipParentId": "OTHER"}, "ClipParentId is another item"),
    ({"Clip": True, "Filename": None}, "no Filename"),
    ({"Clip": True, "SAME_FILE": True}, "shares the source"),
    ({"ClipParentId": "PARENT", "SAME_FILE": True}, "shares the source"),
    ({"Clip": True, "SAME_NAME_NO_FOLDER": True}, "shares the source"),
])
def test_media_item_if_ours_guards(monkeypatch, fake_live, extra, why):
    run, rec = _write_run(fake_live)
    cid = str(uuid.uuid4())
    item = _media(cid, Title=rec["title"])
    extra = dict(extra)
    if extra.pop("SAME_FILE", False):
        item["FolderPathId"], item["Filename"] = run.parent["FolderPathId"], run.parent["Filename"].upper()
    if extra.pop("SAME_NAME_NO_FOLDER", False):
        item["Filename"] = run.parent["Filename"]
        item["FolderPathId"] = None
    if extra.get("ClipParentId") == "PARENT":
        extra["ClipParentId"] = run.parent["Id"]
    if extra.get("ClipParentId") == "OTHER":
        extra["ClipParentId"] = str(uuid.uuid4())
    item.update(extra)
    item = {k: v for k, v in item.items() if v is not None}
    monkeypatch.setattr(server, "ss_get_media_item", _async(lambda i: {"result": item}))
    got, reason = writes._media_item_if_ours(fake_live, run, rec, cid)
    assert got is None and why in reason


def test_media_item_if_ours_accepts_a_clip_in_its_own_file(monkeypatch, fake_live):
    run, rec = _write_run(fake_live)
    cid = str(uuid.uuid4())
    for extra in ({"Clip": True}, {"ClipParentId": run.parent["Id"]},
                  {"Clip": True, "FolderPathId": run.parent["FolderPathId"]}):  # same folder, own file
        item = {**_media(cid, Title=rec["title"]), **extra}
        monkeypatch.setattr(server, "ss_get_media_item", _async(lambda i, item=item: {"result": item}))
        got, reason = writes._media_item_if_ours(fake_live, run, rec, cid)
        assert got is item and reason == ""
    assert writes._media_item_if_ours(fake_live, run, rec, run.parent["Id"])[1] == "id is the source recording"
    run.preexisting.add(cid.lower())
    assert writes._media_item_if_ours(fake_live, run, rec, cid)[1] == "id existed before this run"


def test_find_clip_uses_clips_for_item_first(monkeypatch, fake_live):
    run, rec = _write_run(fake_live)
    cid = str(uuid.uuid4())
    clip = {**_media(cid, Title=rec["title"]), "Clip": True, "ClipParentId": run.parent["Id"]}
    methods = []

    def read(service, method, params=None, compact_output=True, max_chars=0):
        methods.append(method)
        assert max_chars == 200_000
        if method == "GetMyClipsForItem":
            assert params["itemId"] == run.parent["Id"]
            return {"result": [{"Id": str(uuid.uuid4()), "Title": "other"}, {"Id": cid, "Title": rec["title"]}]}
        return {"result": []}

    monkeypatch.setattr(server, "ss_call_read", _async(read))
    monkeypatch.setattr(server, "ss_get_media_item", _async(lambda i: {"result": clip} if i == cid else {"error": "x"}))
    writes._find_clip(fake_live, run, rec, timeout=0)
    assert rec["media_ids"] == [cid] and rec["found_via"] == "Library/GetMyClipsForItem"
    assert methods == ["GetMyClipsForItem"]


def test_find_clip_falls_back_when_clips_for_item_fails(monkeypatch, fake_live):
    run, rec = _write_run(fake_live)
    cid = str(uuid.uuid4())
    clip = {**_media(cid, Title=rec["title"]), "Clip": True}

    def read(service, method, params=None, compact_output=True, max_chars=0):
        if method == "GetMyClipsForItem":
            return {"error": "Unauthorized", "kind": "http", "http_status": 403}
        if method == "GetClipsAndBookmarks":
            return {"result": [{"Id": cid, "ClipTitle": rec["title"]}]}
        return {"result": []}

    monkeypatch.setattr(server, "ss_call_read", _async(read))
    monkeypatch.setattr(server, "ss_get_media_item", _async(lambda i: {"result": clip}))
    writes._find_clip(fake_live, run, rec, timeout=0)
    assert rec["media_ids"] == [cid] and rec["found_via"] == "bookmark id is the clip media item id"
    assert "http_status=403" in rec["clips_for_item_error"]


def test_cleanup_reports_a_render_still_running(monkeypatch, fake_live):
    run, rec = _write_run(fake_live)
    tl = str(uuid.uuid4())
    rec.update({"commit_status": "committed", "task_list_id": tl, "task_state": 1})
    monkeypatch.setattr(writes, "CLEANUP_JOB_WAIT", 0)
    fake_live.firewall = WriteFirewall()
    writes._cleanup(fake_live, run)
    (entry,) = fake_live.report.cleanup
    assert entry["task_list_id"] == tl and "DeleteTaskList" in entry["result"] and "NOT deleted" in entry["result"]


def test_cleanup_does_not_delete_a_non_clip(monkeypatch, fake_live):
    run, rec = _write_run(fake_live)
    cid = str(uuid.uuid4())
    rec.update({"commit_status": "committed", "task_state": 2, "media_ids": [cid]})
    item = _media(cid, Title=rec["title"], Clip=False)
    staged = []
    monkeypatch.setattr(server, "ss_get_media_item", _async(lambda i: {"result": item}))
    monkeypatch.setattr(server, "ss_stage_write", lambda *a, **k: staged.append(a) or {"status": "staged", "token": "t"})
    fake_live.firewall = WriteFirewall()
    writes._cleanup(fake_live, run)
    (entry,) = fake_live.report.cleanup
    assert staged == [] and entry["result"].startswith("NOT deleted: re-check before delete failed (not marked as a clip")
