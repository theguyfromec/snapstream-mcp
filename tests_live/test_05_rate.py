"""PYTEST_DONT_REWRITE

Read tier 6: a burst of cheap reads goes through the token bucket without a 429 reaching the
caller, and takes about as long as the bucket says it should."""

from __future__ import annotations

import asyncio
import time

from snapstream_mcp import server
from tests_live.helpers import describe_error

BURST_CALLS = 30


async def _burst():
    async def one():
        return await server.ss_call_read("Configuration", "GetApiLevel", {"apiType": 0})

    return await asyncio.gather(*(one() for _ in range(BURST_CALLS)))


def test_read_burst_is_paced_without_429(live):
    s = live.rt.settings
    client = live.client
    time.sleep(max(1.0, s.burst / s.rate_per_sec))  # let the bucket refill so the lower bound holds
    retries0, waits0 = client.retries, client.bucket.waits
    t0 = time.perf_counter()
    results = live.run(_burst(), timeout=180)
    elapsed = time.perf_counter() - t0
    errors = [describe_error(r) for r in results if "error" in r]
    surfaced_429 = [r for r in results if r.get("http_status") == 429]
    min_seconds = max(0.0, (BURST_CALLS - s.burst) / s.rate_per_sec)
    max_seconds = min_seconds + BURST_CALLS * 3.0 / s.max_concurrency + 10.0
    live.report.rate = {
        "calls": BURST_CALLS,
        "elapsed_seconds": round(elapsed, 2),
        "bucket_min_seconds": round(min_seconds, 2),
        "allowed_max_seconds": round(max_seconds, 2),
        "read_retries": client.retries - retries0,
        "bucket_waits": client.bucket.waits - waits0,
        "errors": len(errors),
        "surfaced_429": len(surfaced_429),
    }
    for r in results:
        live.assert_no_leaks(r, "burst read")
    assert not surfaced_429, f"{len(surfaced_429)} call(s) surfaced HTTP 429 to the caller"
    assert not errors, f"{len(errors)} burst call(s) failed: {errors[0]}"
    assert elapsed >= min_seconds - 0.25, f"burst took {elapsed:.2f} s, faster than the token bucket allows ({min_seconds:.2f} s)"
    assert elapsed <= max_seconds, f"burst took {elapsed:.2f} s, over the {max_seconds:.2f} s allowance"
