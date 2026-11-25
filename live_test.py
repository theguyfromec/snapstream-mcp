"""Run the live integration suite (tests_live) against the instance in secrets.yaml.

    .venv\\Scripts\\python.exe live_test.py                     read tier only
    .venv\\Scripts\\python.exe live_test.py --writes            read tier + write tier (W1)
    .venv\\Scripts\\python.exe live_test.py --writes --orientation 1   also W2 (social clip)
    .venv\\Scripts\\python.exe live_test.py -k sweep            any extra arguments go to pytest

The suite refuses to run unless secrets.yaml points at demo.cloud.snapstream.com. It prints only
pass, fail and skip lines and the report path; the report holds method names, outcomes, HTTP
statuses, short messages and latencies, never response bodies. Extra arguments cannot turn frame
values back on: with --live the suite overrides --tb=auto/long, -l and --full-trace.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run the SnapStream live integration suite.")
    ap.add_argument("--writes", action="store_true", help="also run the write tier (at most 2 clips, deleted afterwards)")
    ap.add_argument("--orientation", type=int, default=None, help="AiClipOrientation integer for W2 (social clip)")
    ap.add_argument("--report", default=None, help="report base path (default live_reports/<UTC timestamp>)")
    args, extra = ap.parse_known_args(argv)

    if args.report:
        base = Path(args.report)
        if base.suffix.lower() in (".md", ".json"):
            base = base.with_suffix("")
    else:
        base = ROOT / "live_reports" / _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = base.resolve()

    cmd = [
        sys.executable, "-m", "pytest", "tests_live", "--live",
        "-p", "no:cacheprovider", "--tb=short", "-rfEs",
        "--report", str(base),
    ]
    if args.writes:
        cmd.append("--live-writes")
    if args.orientation is not None:
        cmd += ["--orientation", str(args.orientation)]
    cmd += extra
    code = subprocess.call(cmd, cwd=str(ROOT))
    for suffix in (".md", ".json"):
        p = base.with_suffix(suffix)
        print(f"report: {p}" + ("" if p.exists() else " (not written)"))
    return code


if __name__ == "__main__":
    sys.exit(main())
