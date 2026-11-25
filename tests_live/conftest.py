"""PYTEST_DONT_REWRITE

Live integration suite against the SnapStream instance in secrets.yaml. Nothing runs without
--live: every test in tests_live is skipped at collection, so no config is read and no call is
made. The write tier also needs --live-writes; W2 (social clip) also needs --orientation.

Options:
  --live            run the read tier against the configured instance
  --live-writes     also run the write tier (at most 2 commits plus their deletes)
  --orientation N   AiClipOrientation integer for W2 (social clip); W2 skips without it
  --report PATH     report base path (default live_reports/<UTC timestamp>); .md and .json are written

Safety:
  * The suite refuses to run unless secrets.yaml points at the expected host
    (demo.cloud.snapstream.com, or SNAPSTREAM_LIVE_EXPECTED_HOST).
  * A write firewall wraps the client: outside the write tier every write is refused before it
    leaves this machine; inside it only ClipFile/AiClipDesigner (hard cap 2) and DeleteMediaItem
    for ids this run created and verified can pass. The firewall refuses every write unless the
    run was started with --live-writes, and the write tier's fixture skips without it, so the
    tier does not depend on the collection-time skip marker alone (for example under
    -p no:skipping).
  * Failure output leaves out frame values. With --live, pytest_configure forces --tb=short when
    the style is auto or long, and turns off -l/--showlocals and --full-trace, so extra arguments
    passed through live_test.py cannot turn them back on; pytest_runtest_makereport also drops the
    arguments and locals sections from every traceback entry. A failure therefore shows source
    lines and the message the test wrote, not fixture items or response dicts. Assertion
    rewriting is off (PYTEST_DONT_REWRITE), so a bare assert does not print compared values
    either. Every failure report is passed through the server's redactor.
  * A discovery fixture that is missing because a tool returned an error (or discovery raised)
    fails the dependent tests; only a genuinely empty result skips them.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests_live.helpers import (  # noqa: E402
    LiveReport, LiveSession, WriteFirewall, harden_failure_options, install_firewall, short, strip_frame_values,
)

HERE = Path(__file__).resolve().parent
EXPECTED_HOST_ENV = "SNAPSTREAM_LIVE_EXPECTED_HOST"
DEFAULT_EXPECTED_HOST = "demo.cloud.snapstream.com"
# The demo instance's serving alias and the machine name its Library reports (server513).
DEFAULT_ALLOWED_NAMES = ("demo.cloud.snapstream.com", "server513.cloud.snapstream.com")


async def _live_machine_name(rt) -> str:
    """Resolve the tenant's machine name: first Library item -> FolderPathMachineId -> Config name.
    Read-only; returns "" when it cannot be determined."""
    try:
        client = rt.client()
        raw = await client.call("Library", "GetMediaItems", {"index": 0, "count": 1})
        items = raw.get("GetMediaItemsResult") if isinstance(raw, dict) else raw
        if not items:
            return ""
        mid = items[0].get("FolderPathMachineId") or items[0].get("MachineId")
        if not mid:
            return ""
        name = await client.call("Configuration", "GetPublicMachineName", {"machineId": mid})
        name = name.get("GetPublicMachineNameResult") if isinstance(name, dict) else name
        return str(name or "").strip().lower()
    except Exception:  # noqa: BLE001 - any failure means "unknown", which refuses the run
        return ""
REPORT_DIR = ROOT / "live_reports"


class _State:
    live: Optional[LiveSession] = None
    report: Optional[LiveReport] = None
    stamp: str = ""


STATE = _State()


# --------------------------------------------------------------------- options
def pytest_addoption(parser):
    g = parser.getgroup("snapstream-live", "SnapStream live integration suite")
    g.addoption("--live", action="store_true", default=False,
                help="Run tests_live against the instance in secrets.yaml (read tier).")
    g.addoption("--live-writes", action="store_true", default=False,
                help="Also run the write tier (needs --live): at most 2 clip commits, each deleted afterwards.")
    g.addoption("--orientation", type=int, default=None,
                help="AiClipOrientation integer for the W2 social clip test; W2 is skipped without it.")
    g.addoption("--report", default=None,
                help="Report base path; .md and .json are written (default live_reports/<UTC timestamp>).")


def _opt(config, name: str, default: Any = None) -> Any:
    try:
        return config.getoption(name, default=default)
    except ValueError:
        return default


def _in_suite(item) -> bool:
    try:
        return HERE in Path(str(item.path)).resolve().parents
    except Exception:  # noqa: BLE001
        return False


def pytest_configure(config):
    config.addinivalue_line("markers", "live_write: write tier; needs --live and --live-writes")
    if not _opt(config, "--live", False):
        return
    harden_failure_options(config.option)
    now = _dt.datetime.now(_dt.timezone.utc)
    STATE.stamp = now.strftime("%Y%m%dT%H%M%SZ")
    raw = _opt(config, "--report")
    base = Path(raw) if raw else REPORT_DIR / STATE.stamp
    if base.suffix.lower() in (".md", ".json"):
        base = base.with_suffix("")
    STATE.report = LiveReport(base.resolve(), {
        "started": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "run_stamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "writes": bool(_opt(config, "--live-writes", False)),
        "orientation": _opt(config, "--orientation"),
    })


def pytest_collection_modifyitems(config, items):
    live = _opt(config, "--live", False)
    writes = _opt(config, "--live-writes", False)
    skip_all = pytest.mark.skip(reason="live suite: pass --live to run against the configured SnapStream instance")
    skip_writes = pytest.mark.skip(reason="write tier: pass --live-writes (with --live) to run it")
    for item in items:
        if not _in_suite(item):
            continue
        if not live:
            item.add_marker(skip_all)
        elif item.get_closest_marker("live_write") and not writes:
            item.add_marker(skip_writes)


# -------------------------------------------------------------------- fixtures
@pytest.fixture(scope="session")
def live(pytestconfig) -> LiveSession:
    if not _opt(pytestconfig, "--live", False):
        pytest.skip("live suite: pass --live")
    from snapstream_mcp import server
    from snapstream_mcp.config import ConfigError, Settings, load_config

    expected = os.environ.get(EXPECTED_HOST_ENV, "").strip() or DEFAULT_EXPECTED_HOST
    try:
        cfg = load_config()
        settings = Settings.from_env()
    except ConfigError as exc:  # the message names keys and files, never values
        pytest.exit(f"live suite: configuration problem: {exc}", returncode=3)
    # base_url is the shared API gateway (api.snapstream.com) for every tenant; the tenant is chosen
    # by the client_id claim. So check the instance label in secrets.yaml, then confirm the tenant
    # live by resolving the machine name that serves its Library.
    allowed = {h.strip().lower() for h in expected.split(",") if h.strip()} | (
        set(DEFAULT_ALLOWED_NAMES) if expected == DEFAULT_EXPECTED_HOST else set()
    )
    label = (cfg.instance or "").strip().lower()
    if label not in allowed:
        pytest.exit(
            f"live suite: secrets.yaml instance is {label or '(not set)'}, not one of {sorted(allowed)}. "
            f"The suite only runs against the demo instance (set {EXPECTED_HOST_ENV} to override).",
            returncode=3,
        )
    loop = asyncio.new_event_loop()
    rt = server.Runtime(config=cfg, settings=settings)
    server.set_runtime(rt)
    machine = loop.run_until_complete(_live_machine_name(rt))
    if machine not in allowed:
        loop.run_until_complete(rt.aclose())
        loop.close()
        pytest.exit(
            f"live suite: the credentials reach {machine or '(unknown machine)'}, not one of {sorted(allowed)}. "
            f"Refusing to run (set {EXPECTED_HOST_ENV} to override).",
            returncode=3,
        )
    fw = WriteFirewall(max_creates=2, writes_allowed=bool(_opt(pytestconfig, "--live-writes", False)))
    report = STATE.report or LiveReport(REPORT_DIR / "unnamed", {})
    report.meta["host"] = cfg.base_host
    report.meta["machine"] = machine
    session = LiveSession(
        rt, loop, fw, report,
        run_stamp=report.meta.get("run_stamp", ""),
        expected_host=expected,
        options={
            "writes": bool(_opt(pytestconfig, "--live-writes", False)),
            "orientation": _opt(pytestconfig, "--orientation"),
            "allowed_names": sorted(allowed),
        },
    )
    install_firewall(rt.client(), rt.catalog, fw, session.call_log)
    STATE.live = session
    yield session
    try:
        report.fixtures = {
            **{k: ("found" if v else "not found") for k, v in session.discovery.found().items()},
            **{f"{k} (reason)": v for k, v in session.discovery.reasons.items()},
            **{f"{k} (tool error)": v for k, v in session.discovery.errors.items()},
        }
        if fw.violations:
            report.sweep_notes.append(f"write firewall refused {len(fw.violations)} call(s): " + "; ".join(fw.violations))
        loop.run_until_complete(rt.aclose())
    finally:
        server.set_runtime(None)
        loop.close()


@pytest.fixture(autouse=True)
def _firewall_clean(request):
    """Fail any live test during which the write firewall refused a call."""
    if not _opt(request.config, "--live", False) or STATE.live is None:
        yield
        return
    before = len(STATE.live.firewall.violations)
    yield
    new = STATE.live.firewall.violations[before:]
    assert not new, "write firewall refused: " + "; ".join(new)


@pytest.fixture(scope="module", autouse=True)
def _no_pending_writes(request):
    """Staging never commits in the read tier: at module end nothing may be pending. Anything
    left is canceled first, then the module fails."""
    yield
    if not _opt(request.config, "--live", False) or STATE.live is None:
        return
    from snapstream_mcp import server

    pending = server.ss_pending_writes()
    left = [p.get("method") for p in pending.get("pending", [])]
    for p in pending.get("pending", []):
        server.ss_cancel_staged(p["token"])
    assert not left, f"{len(left)} staged write(s) left pending at module end (canceled now): {', '.join(map(str, left))}"


def _need(live: LiveSession, name: str, value: Any, what: str) -> Any:
    """The fixture value; when missing, fail on a tool error and skip on a genuinely empty result."""
    if value is not None:
        return value
    reason = live.discovery.reasons.get(name, "not found")
    if live.discovery.tool_error(name):
        pytest.fail(f"{what} fixture: {reason}", pytrace=False)
    pytest.skip(f"no {what} fixture: {reason}")


@pytest.fixture(scope="session")
def media_item(live) -> Dict[str, Any]:
    return _need(live, "media_item", live.discovery.media_item(), "media item")


@pytest.fixture(scope="session")
def channel_id(live) -> str:
    return _need(live, "channel_id", live.discovery.channel_id(), "channel")


@pytest.fixture(scope="session")
def airing(live) -> Dict[str, Any]:
    return _need(live, "airing", live.discovery.airing(), "airing")


@pytest.fixture(scope="session")
def job_id(live) -> str:
    return _need(live, "job_id", live.discovery.job_id(), "recording job")


@pytest.fixture(scope="session")
def task_list_id(live) -> str:
    return _need(live, "task_list_id", live.discovery.task_list_id(), "task list")


@pytest.fixture(scope="session")
def bookmark_id(live) -> str:
    return _need(live, "bookmark_id", live.discovery.bookmark_id(), "clip or bookmark")


# ---------------------------------------------------------------- reporting
def _scrub(text: str) -> str:
    return STATE.live.scrub(text) if STATE.live is not None else text


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    if not _opt(item.config, "--live", False) or not _in_suite(item):
        return
    if rep.failed and rep.longrepr is not None:
        strip_frame_values(rep.longrepr)
    if STATE.live is None:
        return
    if rep.longrepr is not None and not rep.skipped:
        text = str(rep.longrepr)
        clean = _scrub(text)
        if clean != text:
            rep.longrepr = clean
    if rep.sections:
        rep.sections = [(title, _scrub(content)) for title, content in rep.sections]


def pytest_runtest_logreport(report):
    if STATE.report is None:
        return
    if report.when == "call" or (report.when == "setup" and report.outcome != "passed") or (
        report.when == "teardown" and report.outcome == "failed"
    ):
        note = ""
        if report.skipped and isinstance(report.longrepr, tuple) and len(report.longrepr) == 3:
            note = short(report.longrepr[2], 200)
        elif report.failed:
            last = (report.longreprtext or "").strip().splitlines()
            note = short(_scrub(last[-1] if last else "failed"), 200)
            if report.when != "call":
                note = f"[{report.when}] {note}"
        STATE.report.tests.append({"nodeid": report.nodeid, "outcome": report.outcome, "note": note})


def pytest_sessionfinish(session, exitstatus):
    if STATE.report is None:
        return
    STATE.report.meta["finished"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    STATE.report.meta["exit_status"] = int(exitstatus)
    try:
        STATE.report.write()
    except OSError as exc:
        STATE.report.meta["write_error"] = type(exc).__name__


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if STATE.report is None:
        return
    terminalreporter.write_line(f"live report: {STATE.report.md_path}")
    terminalreporter.write_line(f"live report (json): {STATE.report.json_path}")
