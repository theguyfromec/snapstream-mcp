"""PYTEST_DONT_REWRITE

Read tier 4: call EVERY catalog method with access=read and spec read-only true once, through
ss_call_read, with params built from its schema and the discovered ids. Each outcome goes into
the report (method, outcome, HTTP status, short message, latency; never a response body).

Fails on: HTTP 404 (path bug), timeouts and network errors, an auth/config/budget failure, a
local crash in the server code, or a secret-looking value in any output. API faults from
synthetic parameters are recorded, not failed. Reads whose spec read-only flag is false are
skipped and listed."""

from __future__ import annotations

from snapstream_mcp import server
from tests_live.helpers import SWEEP_FAIL_OUTCOMES, build_sweep_params, classify, result_of, shape_issues, short, timed


def test_catalog_read_sweep(live):
    rt = live.rt
    report = live.report
    ids = live.discovery.sweep_ids()
    missing = sorted(k for k, v in ids.items() if v is None)
    if missing:
        report.sweep_notes.append("ids with no fixture (synthetic GUIDs used): " + ", ".join(missing))
    methods = sorted((m for m in rt.catalog.all() if m.access == "read"), key=lambda m: m.key)
    report.sweep_skipped.extend({"method": m.key} for m in methods if m.read_only is not True)
    failures = []
    for info in (m for m in methods if m.read_only is True):
        params, synthetic = build_sweep_params(info, ids, rt.catalog)
        elapsed = timed()
        try:
            out = live.run(server.ss_call_read(info.service, info.name, params, max_chars=4000))
        except Exception as exc:  # noqa: BLE001 - anything escaping the tool is a local crash
            out = None
            outcome, status, msg = "local_crash", None, f"{type(exc).__name__} escaped ss_call_read"
        else:
            outcome, status, msg = classify(out)
        latency = elapsed()
        if out is not None:
            leaks = live.leaks(out)
            if leaks:
                outcome, msg = "secret_leak", ", ".join(leaks)
        if outcome == "ok":
            ticks = set(rt.catalog.tick_fields) | ({"Start", "End"} if info.name == "GetSmartChapters" else set())
            issues = shape_issues(result_of(out), ticks)
            if issues:
                msg = "shape: " + short("; ".join(issues[:3]))
        if synthetic and outcome != "ok":
            msg = short(f"[synthetic {', '.join(synthetic)}] {msg}")
        row = {"method": info.key, "outcome": outcome, "http_status": status, "message": msg, "latency_ms": latency}
        report.sweep.append(row)
        if outcome in SWEEP_FAIL_OUTCOMES:
            failures.append(f"{info.key}: {outcome}" + (f" (HTTP {status})" if status else ""))
        if outcome == "budget":
            break  # the daily budget is spent: stop instead of failing every remaining call
    report.sweep_notes.append(
        f"{len(report.sweep)} read-only reads called, {len(report.sweep_skipped)} reads skipped (spec read-only false)"
    )
    assert not failures, f"{len(failures)} sweep failure(s): " + "; ".join(failures[:30])
