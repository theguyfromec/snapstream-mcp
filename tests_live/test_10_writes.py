"""PYTEST_DONT_REWRITE

Write tier (needs --live AND --live-writes). On the shared demo instance, as the single API user
the client maps to:

  W1  standard clip: ss_stage_clip kind "clip", 10 s from 60 s in, commit, poll the task list,
      find the clip under its parent, check title and length.
  W2  social clip (only with --orientation): the same through kind "social", Subtitles true.

The tier is gated twice on --live-writes: the write_run fixture skips without it, and the write
firewall refuses every write unless the run was started with it. The collection-time skip marker
is a third gate, not the only one.

Cleanup runs in a module finalizer even when a test fails. It deletes ONLY media items this run
created: the id must have been recorded by this run, its title must be exactly the run's title
(prefix "MCP live test " plus the run timestamp), it must be marked as a clip of the fixture item
(Clip true, or ClipParentId equal to the parent), and its file (FolderPathId and Filename) must
differ from the parent's. All of it is re-checked with GetMediaItem right before the delete. A
render still running at cleanup is waited for (bounded); if it is still running after that, it is
reported with its task list id for a manual cancel, never deleted. Deletes go through
ss_stage_write + ss_commit_write on Library/DeleteMediaItem. The write firewall caps the run at 2
create commits and one delete per create; nothing else can be sent."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

import pytest

from snapstream_mcp import server
from tests_live.helpers import TITLE_PREFIX, as_list, describe_error, result_of, titles_of, usable_guid

pytestmark = pytest.mark.live_write

CLIP_START = 60
CLIP_END = 70
CLIP_SECONDS = CLIP_END - CLIP_START
LENGTH_TOLERANCE = 3.0
# Clip render time is 2 to 7 minutes (source review); a social render adds AI design and subtitles.
JOB_TIMEOUTS = {"clip": 480, "social": 600}
CLEANUP_JOB_WAIT = 300    # extra wait in cleanup for a render the test stopped waiting for
FIND_TIMEOUT = 90
POLL_SECONDS = 5
MAX_CHARS = 200_000
SUCCEEDED, FAILED = 2, 3  # TaskListState
TERMINAL = (SUCCEEDED, FAILED)


class WriteRun:
    """What this run created, for the assertions and for cleanup."""

    def __init__(self, live, parent: Dict[str, Any]) -> None:
        self.live = live
        self.parent = parent
        self.records: List[Dict[str, Any]] = []
        self.preexisting: set = set()

    def title(self, suffix: str = "") -> str:
        return f"{TITLE_PREFIX}{self.live.run_stamp}{suffix}"

    def is_ours(self, title: Any) -> bool:
        return (
            isinstance(title, str)
            and title.startswith(TITLE_PREFIX)
            and self.live.run_stamp in title
            and any(title == r["title"] for r in self.records)
        )


# ------------------------------------------------------------------ helpers
def _paged(live, service: str, method: str, params: Dict[str, Any], *, page: int,
           max_items: int, max_pages: int = 12) -> Tuple[List[Any], Optional[str]]:
    """Items from a paged read, following output capping: when a page comes back truncated the
    index advances by the items actually returned and the next page is read, so a capped page
    never ends the scan early. Returns (items, error description or None)."""
    items: List[Any] = []
    index = 0
    for _ in range(max_pages):
        out = live.run(server.ss_call_read(service, method, {**params, "index": index, "count": page}, max_chars=MAX_CHARS))
        if not isinstance(out, dict) or "error" in out:
            return items, f"{service}/{method}: {describe_error(out)}"
        got = as_list(result_of(out))
        items += got
        if out.get("truncated"):
            n = out.get("items_returned")
            n = n if isinstance(n, int) else len(got)
            if n <= 0:
                return items, f"{service}/{method}: one item is larger than the output cap at index {index}"
            index += n
        elif len(got) < page:
            break
        else:
            index += len(got)
        if len(items) >= max_items:
            break
    return items, None


def _clip_list_ids(live, parent_id: str) -> Tuple[set, Optional[str]]:
    """Ids of the clips and bookmarks under parent_id (GetClipsAndBookmarks, every page)."""
    marks, err = _paged(live, "Library", "GetClipsAndBookmarks", {"parentId": parent_id}, page=1000, max_items=5000)
    return {str(b.get("Id")).lower() for b in marks if isinstance(b, dict) and b.get("Id")}, err


def _poll_job(live, list_id: str, timeout: float) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout
    state: Optional[int] = None
    polls = 0
    while time.monotonic() < deadline:
        polls += 1
        out = live.run(server.ss_get_job(list_id))
        tl = result_of(out)
        if "error" not in out and isinstance(tl, dict):
            state = tl.get("State") if isinstance(tl.get("State"), int) else state
            if state in TERMINAL:
                break
        time.sleep(POLL_SECONDS)
    return {"state": state, "polls": polls}


def _same_file(item: Dict[str, Any], parent: Dict[str, Any]) -> bool:
    """True when the item's file is (or may be) the parent's: the same Filename, and the same
    FolderPathId or none to compare."""
    fn, pfn = item.get("Filename"), parent.get("Filename")
    if not (isinstance(fn, str) and isinstance(pfn, str)) or fn.lower() != pfn.lower():
        return False
    fp, pfp = item.get("FolderPathId"), parent.get("FolderPathId")
    return not (usable_guid(fp) and usable_guid(pfp)) or fp.lower() == pfp.lower()


def _media_item_if_ours(live, run: WriteRun, rec: Dict[str, Any], item_id: Any) -> Tuple[Optional[Dict[str, Any]], str]:
    """(GetMediaItem(item_id), "") when it is the clip this record made, else (None, why): exact
    title of this run, not the parent, not a clip that existed before the run, marked as a clip
    of the parent, and stored in a different file from the parent."""
    parent_id = run.parent["Id"].lower()
    if not usable_guid(item_id):
        return None, "no usable id"
    if item_id.lower() == parent_id:
        return None, "id is the source recording"
    if item_id.lower() in run.preexisting:
        return None, "id existed before this run"
    out = live.run(server.ss_get_media_item(item_id))
    item = result_of(out)
    if "error" in out or not isinstance(item, dict):
        return None, f"GetMediaItem failed ({describe_error(out)})"
    if rec["title"] not in titles_of(item) or not run.is_ours(rec["title"]):
        return None, "title is not this run's"
    clip_parent = item.get("ClipParentId")
    if usable_guid(clip_parent) and clip_parent.lower() != parent_id:
        return None, "ClipParentId is another item"
    if not (item.get("Clip") is True or (usable_guid(clip_parent) and clip_parent.lower() == parent_id)):
        return None, "not marked as a clip of the source recording"
    if not isinstance(item.get("Filename"), str) or not item["Filename"]:
        return None, "no Filename to compare with the source recording's file"
    if _same_file(item, run.parent):
        return None, "shares the source recording's file"
    return item, ""


def _take(rec: Dict[str, Any], item: Dict[str, Any], via: str) -> None:
    rec["media_ids"] = [item["Id"]]
    rec["found_via"] = via
    rec["clip_item"] = item


def _find_clip(live, run: WriteRun, rec: Dict[str, Any], timeout: float = FIND_TIMEOUT) -> None:
    """Fill rec['media_ids'], rec['bookmark_ids'] and rec['found_via'] for this record's clip.

    Order: Library/GetMyClipsForItem (the direct per-parent clip list, MediaItem[]), then the
    bookmarks under the parent (GetClipsAndBookmarks, on the hypothesis that a clip's bookmark id
    is its media item id), polled until timeout; then a scan of the newest Library pages. Both
    clip-list methods are valid_ticket_only and can fail through the gateway on builds without
    SCRUM-7181, so a failure there falls back instead of failing."""
    deadline = time.monotonic() + timeout
    parent_id = run.parent["Id"]
    rec.setdefault("bookmark_ids", [])
    rejected: List[str] = []
    clips_for_item = True
    while True:
        if clips_for_item:
            items, err = _paged(live, "Library", "GetMyClipsForItem", {"itemId": parent_id}, page=1000, max_items=5000)
            if err:
                clips_for_item = False
                rec["clips_for_item_error"] = err
            for it in items:
                if not isinstance(it, dict) or rec["title"] not in titles_of(it):
                    continue
                cp = it.get("ClipParentId")
                if usable_guid(cp) and cp.lower() != parent_id.lower():
                    continue
                item, why = _media_item_if_ours(live, run, rec, it.get("Id"))
                if item is not None:
                    _take(rec, item, "Library/GetMyClipsForItem")
                    return
                rejected.append(why)
        marks_all, err = _paged(live, "Library", "GetClipsAndBookmarks", {"parentId": parent_id}, page=1000, max_items=5000)
        if err:
            rec["bookmarks_error"] = err
        marks = [
            b for b in marks_all
            if isinstance(b, dict) and rec["title"] in titles_of(b) and str(b.get("Id", "")).lower() not in run.preexisting
        ]
        if rec.get("task_list_id"):
            exact = [b for b in marks if str(b.get("TaskListId", "")).lower() == rec["task_list_id"].lower()]
            marks = exact or marks
        rec["bookmark_ids"] = sorted({b["Id"] for b in marks if usable_guid(b.get("Id"))})
        for bid in rec["bookmark_ids"]:
            item, why = _media_item_if_ours(live, run, rec, bid)
            if item is not None:
                _take(rec, item, "bookmark id is the clip media item id")
                return
            rejected.append(why)
        if marks or time.monotonic() >= deadline:
            break
        time.sleep(POLL_SECONDS)
    # Fallback: scan the newest Library pages for a clip with this exact title and parent.
    items, err = _paged(live, "Library", "GetMediaItems", {}, page=100, max_items=400)
    if err:
        rec["library_scan_error"] = err
    found: Dict[str, Dict[str, Any]] = {}
    for it in items:
        if isinstance(it, dict) and rec["title"] in titles_of(it) and usable_guid(it.get("Id")):
            if it["Id"].lower() in found:
                continue
            item, why = _media_item_if_ours(live, run, rec, it["Id"])
            if item is not None:
                found[it["Id"].lower()] = item
            else:
                rejected.append(why)
    if found:
        rec["media_ids"] = [it["Id"] for it in found.values()]
        rec["found_via"] = "Library/GetMediaItems scan"
        rec["clip_item"] = next(iter(found.values()))
    else:
        rec.setdefault("media_ids", [])
        rec["found_via"] = "not found" + (" (bookmark only)" if rec["bookmark_ids"] else "")
        if rejected:
            rec["found_via"] += "; title matched but rejected: " + "; ".join(sorted(set(rejected)))


def _length_seconds(rec: Dict[str, Any]) -> Optional[float]:
    item = rec.get("clip_item") or {}
    d = item.get("Duration_seconds")
    return float(d) if isinstance(d, (int, float)) else None


def _cleanup(live, run: WriteRun) -> None:
    report = live.report
    fw = live.firewall
    for rec in run.records:
        if rec.get("commit_status") in (None, "refused", "aborted") or rec.get("commit_kind") == "test_firewall":
            report.cleanup.append({"title": rec["title"], "result": "nothing to delete (no commit was sent)"})
            continue
        tlid = rec.get("task_list_id")
        if usable_guid(tlid) and rec.get("task_state") not in TERMINAL:
            job = _poll_job(live, tlid, CLEANUP_JOB_WAIT)
            rec["task_state"] = job["state"]
            if job["state"] not in TERMINAL:
                report.cleanup.append({
                    "title": rec["title"], "task_list_id": tlid, "task_state": job["state"],
                    "result": (
                        f"NOT deleted: render still running after a further {CLEANUP_JOB_WAIT} s. Cancel it with "
                        "TaskManager/DeleteTaskList on this task_list_id (not StopTask), then find any clip by "
                        "title and delete it by hand"
                    ),
                })
                continue
        if not rec.get("media_ids"):
            try:
                _find_clip(live, run, rec, timeout=30)
            except Exception as exc:  # noqa: BLE001
                rec["found_via"] = f"lookup failed: {type(exc).__name__}"
        ids = list(dict.fromkeys(rec.get("media_ids") or []))
        if not ids:
            report.cleanup.append({
                "title": rec["title"], "task_list_id": tlid, "bookmark_ids": rec.get("bookmark_ids"),
                "found_via": rec.get("found_via"),
                "result": "NOT deleted: clip media item not found; check by title and delete by hand",
            })
            continue
        if len(ids) > 1:
            report.cleanup.append({
                "title": rec["title"], "media_ids": ids,
                "result": "NOT deleted: more than one media item matched; check by hand",
            })
            continue
        item_id = ids[0]
        entry: Dict[str, Any] = {"title": rec["title"], "media_id": item_id}
        try:
            item, why = _media_item_if_ours(live, run, rec, item_id)
            if item is None:
                entry["result"] = f"NOT deleted: re-check before delete failed ({why}); check by hand"
                continue
            staged = server.ss_stage_write("Library", "DeleteMediaItem", {"itemId": item_id}, reason="live test cleanup")
            if staged.get("status") != "staged":
                entry["result"] = f"NOT deleted: stage refused ({describe_error(staged)})"
                continue
            fw.allow_delete(item_id)
            res = live.run(server.ss_commit_write(staged["token"], None))
            entry["commit_status"] = res.get("status")
            if res.get("status") != "committed":
                entry["result"] = f"delete not confirmed ({describe_error(res)})"
                continue
            after = live.run(server.ss_get_media_item(item_id))
            gone = "error" in after or not isinstance(result_of(after), dict) or result_of(after).get("Deleted") is True
            entry["result"] = "deleted" if gone else "delete committed but GetMediaItem still returns the item"
            left, err = _clip_list_ids(live, run.parent["Id"])
            if err:
                entry["bookmark_check"] = err
            still = [b for b in rec.get("bookmark_ids") or [] if b.lower() in left]
            if still:
                entry["bookmark_still_listed"] = still
        except Exception as exc:  # noqa: BLE001 - record and keep cleaning the rest
            entry["result"] = f"NOT deleted: {type(exc).__name__}"
        finally:
            report.cleanup.append(entry)


@pytest.fixture(scope="module")
def write_run(request, pytestconfig):
    # Not only the collection-time marker: with the skipping plugin off, marks are never
    # evaluated, so the option is checked here too, before any discovery (pytest.skip is an
    # exception, which works without that plugin). The firewall checks it again on every write.
    if not pytestconfig.getoption("--live-writes", default=False):
        pytest.skip("write tier: pass --live-writes (with --live) to run it")
    live = request.getfixturevalue("live")
    if not (live.options.get("writes") and live.firewall.writes_allowed):
        pytest.skip("write tier: pass --live-writes (with --live) to run it")
    media_item = request.getfixturevalue("media_item")
    dur = media_item.get("Duration_seconds") or 0
    if dur < CLIP_END + 5:
        pytest.skip("fixture media item is too short for a clip at 60-70 s")
    run = WriteRun(live, media_item)
    marks, err = _clip_list_ids(live, media_item["Id"])
    if err:
        pytest.fail(f"could not list the clips that already exist under the fixture item: {err}", pytrace=False)
    clips, _err = _paged(live, "Library", "GetMyClipsForItem", {"itemId": media_item["Id"]}, page=1000, max_items=5000)
    run.preexisting = marks | {str(c.get("Id")).lower() for c in clips if isinstance(c, dict) and c.get("Id")}
    live.firewall.enabled = True
    try:
        yield run
    finally:
        try:
            _cleanup(live, run)
        finally:
            live.firewall.enabled = False
            leftover = server.ss_pending_writes()
            for p in leftover.get("pending", []):
                server.ss_cancel_staged(p["token"])


def _clip_flow(live, run: WriteRun, *, kind: str, suffix: str, needles=(), **stage_kwargs) -> Dict[str, Any]:
    title = run.title(suffix)
    rec: Dict[str, Any] = {"kind": kind, "title": title}
    view: Dict[str, Any] = {"kind": kind, "title": title}
    run.records.append(rec)
    live.report.writes.append(view)

    staged = live.run(server.ss_stage_clip(
        run.parent["Id"], CLIP_START, CLIP_END, title, kind=kind, reason=f"live integration test {kind}", **stage_kwargs
    ))
    assert staged.get("status") == "staged", f"ss_stage_clip ({kind}) did not stage: {describe_error(staged)}"
    preview = staged["preview"]
    for needle, what in (
        (str(CLIP_START * 10_000_000), "start ticks"), (str(CLIP_END * 10_000_000), "end ticks"),
        ("00:01:00", "start hh:mm:ss"), ("00:01:10", "end hh:mm:ss"),
        ("mapped to this API client ID", "mapped-user line"),
        *needles,
    ):
        assert needle in preview, f"preview has no {what}"
    live.assert_no_leaks(staged, "ss_stage_clip")

    # Mark the attempt first: if the commit times out, cleanup still looks for the clip.
    rec["commit_status"] = view["commit_status"] = "sent"
    res = live.run(server.ss_commit_write(staged["token"], None), timeout=120)
    rec["commit_status"] = view["commit_status"] = res.get("status")
    rec["commit_kind"] = res.get("kind")
    live.assert_no_leaks(res, "ss_commit_write")
    assert res.get("status") == "committed", f"commit did not succeed: {describe_error(res)}"
    list_id = result_of(res)
    assert usable_guid(list_id), "commit returned no task list GUID"
    rec["task_list_id"] = view["task_list_id"] = list_id

    timeout = JOB_TIMEOUTS.get(kind, max(JOB_TIMEOUTS.values()))
    job = _poll_job(live, list_id, timeout)
    rec["task_state"] = job["state"]
    view.update({"task_state": job["state"], "polls": job["polls"]})
    assert job["state"] == SUCCEEDED, (
        f"task list ended in state {job['state']} (2 = Succeeded) after {job['polls']} polls ({timeout} s limit)"
    )

    _find_clip(live, run, rec)
    view.update({"media_ids": rec.get("media_ids"), "bookmark_ids": rec.get("bookmark_ids"), "found_via": rec.get("found_via")})
    assert rec.get("media_ids"), f"new clip not found under its parent ({rec.get('found_via')})"
    assert rec["title"] in titles_of(rec["clip_item"]), "clip title does not match"
    length = _length_seconds(rec)
    view["length_seconds"] = length
    assert length is not None, "clip has no Duration"
    assert abs(length - CLIP_SECONDS) <= LENGTH_TOLERANCE, f"clip is {length:.1f} s, expected about {CLIP_SECONDS} s"
    return rec


def test_w1_standard_clip(live, write_run):
    _clip_flow(live, write_run, kind="clip", suffix="")


def test_w2_social_clip(live, write_run):
    orientation = live.options.get("orientation")
    if orientation is None:
        pytest.skip("W2 needs --orientation (the AiClipOrientation value is unresolved; pass the one to test)")
    _clip_flow(
        live, write_run, kind="social", suffix=" social", orientation=int(orientation), subtitles=True,
        needles=((f"Orientation: {int(orientation)}", "orientation line"), ("Subtitles:   True", "subtitles line")),
    )
