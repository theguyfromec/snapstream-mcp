"""Manual, READ-ONLY live check against the instance in secrets.yaml.

Run by hand only, against the demo instance:
    .venv\\Scripts\\python.exe smoke_test.py

Steps: token request, ss_status, Library/GetMediaItems (count 2), Search/SearchRecordings
(count 2), and Library/GetTranscript on the first media item (truncated). It prints only pass or
fail, HTTP status, timings and the SHAPE of each response (key names and types). It never prints
credential values, tokens, or response values. It makes no write calls.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from snapstream_mcp import server  # noqa: E402
from snapstream_mcp.client import SnapStreamError  # noqa: E402
from snapstream_mcp.config import ConfigError, Settings, load_config  # noqa: E402
from snapstream_mcp.formatting import shape  # noqa: E402

READ_ONLY_CALLS = {("Library", "GetMediaItems"), ("Search", "SearchRecordings"), ("Library", "GetTranscript")}


def show(label: str, ok: bool, detail: object = "") -> None:
    mark = "PASS" if ok else "FAIL"
    text = detail if isinstance(detail, str) else json.dumps(detail, indent=2)
    print(f"[{mark}] {label}" + (f"\n{text}" if text else ""))


def _first_id(items: object) -> str | None:
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict):
                for k in ("Id", "id", "ID", "ItemId", "MediaItemId"):
                    v = it.get(k)
                    if isinstance(v, str) and len(v) == 36:
                        return v
    return None


async def main() -> int:
    try:
        cfg = load_config()
    except ConfigError as exc:
        show("config", False, str(exc))
        return 2
    rt = server.Runtime(config=cfg, settings=Settings.from_env())
    server.set_runtime(rt)
    client = rt.client()
    failures = 0
    try:
        t0 = time.perf_counter()
        try:
            await client.get_token()
            show("token", True, f"obtained in {time.perf_counter() - t0:.2f} s, expires in {client.token_seconds_left()} s")
        except SnapStreamError as exc:
            show("token", False, exc.to_dict())
            return 1

        st = await server.ss_status(check_token=False)
        show("ss_status", st.get("config", {}).get("ok", False), {
            "base_host_set": bool(st.get("base_host")),
            "catalog": st.get("catalog"),
            "pending_writes": st.get("pending_writes"),
        })

        async def probe(service: str, method: str, params: dict) -> object:
            nonlocal failures
            assert (service, method) in READ_ONLY_CALLS
            assert rt.catalog.access_of(service, method) == "read"
            t = time.perf_counter()
            try:
                raw = await client.call(service, method, params)
            except SnapStreamError as exc:
                failures += 1
                show(f"{service}/{method}", False, {"kind": exc.kind, "http_status": exc.status})
                return None
            show(f"{service}/{method}", True, {"seconds": round(time.perf_counter() - t, 2), "shape": shape(raw, max_depth=4)})
            return raw

        items = await probe("Library", "GetMediaItems", {"index": 0, "count": 2})
        await probe("Search", "SearchRecordings", {
            "search": "news", "index": 0, "count": 2, "type": 4,
            "highlightStart": "<em>", "highlightEnd": "</em>",
        })

        from snapstream_mcp.formatting import unwrap_result

        item_id = _first_id(unwrap_result(items, "GetMediaItems")) if items is not None else None
        if item_id is None:
            show("Library/GetTranscript", False, "skipped: no media item id found in GetMediaItems")
        else:
            out = await server.ss_get_transcript(item_id, max_chars=600)
            ok = "error" not in out
            failures += 0 if ok else 1
            show("ss_get_transcript (first item, 600 chars)", ok, {
                "caption_count": out.get("caption_count"),
                "returned_lines": out.get("returned_lines"),
                "time_basis": out.get("time_basis"),
                "caption_fields": out.get("caption_fields"),
                "truncated": out.get("truncated"),
                "error_kind": out.get("kind"),
                "http_status": out.get("http_status"),
            })
    finally:
        await rt.aclose()
    print("done:", "all checks passed" if failures == 0 else f"{failures} check(s) failed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
