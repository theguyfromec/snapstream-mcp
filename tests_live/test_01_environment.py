"""PYTEST_DONT_REWRITE

Read tier 1: environment (config, token, identity, gateway path mapping, call counter) and the
MCP protocol path over stdio."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict

from snapstream_mcp import server
from tests_live.helpers import describe_error

ROOT = Path(__file__).resolve().parent.parent
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
VERSION_RE = re.compile(r"^\d+\.\d+")
CORE_TOOLS = {
    "ss_status", "ss_api_catalog", "ss_api_describe", "ss_call_read", "ss_stage_write", "ss_stage_clip",
    "ss_commit_write", "ss_cancel_staged", "ss_pending_writes", "ss_search_recordings", "ss_get_transcript",
    "ss_list_media_items", "ss_whoami",
}


def test_status_config_and_token(live):
    out = live.run(server.ss_status(check_token=True))
    assert out.get("config", {}).get("ok") is True, "ss_status: config is not ok"
    tok = out.get("token") or {}
    assert tok.get("ok") is True, f"ss_status: token request failed ({describe_error(tok)})"
    assert int(tok.get("expires_in_seconds") or 0) > 60, "ss_status: token expires in 60 s or less"
    assert out.get("base_host"), "ss_status: base_host is empty"
    assert str(out.get("instance") or "").lower() in live.options["allowed_names"], (
        "ss_status: instance label is not the demo instance"
    )
    assert out.get("catalog", {}).get("read", 0) > 0, "ss_status: catalog has no read methods"
    live.assert_no_leaks(out, "ss_status")


def test_whoami_returns_an_email(live):
    out = live.assert_ok(live.run(server.ss_whoami()), "ss_whoami")
    email = out.get("result")
    assert isinstance(email, str) and EMAIL_RE.match(email), "AuthenticationManager/GetUserEmail did not return an email address"


def test_api_level_proves_url_path_mapping(live):
    # Configuration is served under the gateway path Config; a 404 here means the mapping broke.
    for svc in ("Configuration", "Config"):
        out = live.assert_ok(live.run(server.ss_call_read(svc, "GetApiLevel", {"apiType": 0})), f"{svc}/GetApiLevel")
        assert out.get("method") == "Configuration/GetApiLevel", f"{svc}/GetApiLevel resolved to {out.get('method')}"
        v = out.get("result")
        assert isinstance(v, str) and VERSION_RE.match(v), f"{svc}/GetApiLevel did not return a version string"


def test_calls_today_counter_increments(live):
    before = live.usage()["calls_today"]
    live.assert_ok(live.run(server.ss_whoami()), "ss_whoami")
    after = live.usage()["calls_today"]
    assert after >= before + 1, f"calls_today did not increase ({before} -> {after})"
    st = live.run(server.ss_status(check_token=False))
    assert st.get("usage", {}).get("calls_today", -1) >= after, "ss_status usage.calls_today lags the client counter"


# ------------------------------------------------------------------ stdio protocol path
def _payload(res: Any) -> Dict[str, Any]:
    sc = getattr(res, "structured_content", None)
    if isinstance(sc, dict):
        if set(sc) == {"result"} and isinstance(sc["result"], dict):
            return sc["result"]
        return sc
    for c in getattr(res, "content", None) or []:
        text = getattr(c, "text", None)
        if isinstance(text, str):
            try:
                data = json.loads(text)
            except ValueError:
                continue
            if isinstance(data, dict):
                return data
    return {}


def _texts(res: Any) -> str:
    return " ".join(getattr(c, "text", "") or "" for c in (getattr(res, "content", None) or []))


async def _stdio_roundtrip() -> Dict[str, Any]:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command=sys.executable, args=["-m", "snapstream_mcp"], cwd=str(ROOT), env=dict(os.environ)
    )
    with open(os.devnull, "w") as errlog:  # server stderr is not shown: it could hold anything
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                tools = await session.list_tools()
                status = await session.call_tool("ss_status", {"check_token": True})
                search = await session.call_tool(
                    "ss_search_recordings", {"search": "news", "count": 1, "type": "title"}
                )
    info = getattr(init, "server_info", None) or getattr(init, "serverInfo", None)
    return {
        "server_name": getattr(info, "name", None),
        "tools": {t.name: t for t in tools.tools},
        "status": status,
        "search": search,
    }


def test_stdio_protocol_path(live):
    out = live.run(_stdio_roundtrip(), timeout=180)
    assert out["server_name"] == "snapstream", f"initialize: server name is {out['server_name']!r}"
    missing = sorted(CORE_TOOLS - set(out["tools"]))
    assert not missing, f"list_tools: missing {', '.join(missing)}"
    commit = out["tools"]["ss_commit_write"]
    assert getattr(commit.annotations, "destructive_hint", None) is True, "ss_commit_write is not annotated destructive"

    status, search = out["status"], out["search"]
    assert not getattr(status, "is_error", False), "ss_status over stdio returned isError"
    st = _payload(status)
    assert st.get("config", {}).get("ok") is True, "ss_status over stdio: config is not ok"
    assert (st.get("token") or {}).get("ok") is True, f"ss_status over stdio: token failed ({describe_error(st.get('token'))})"
    assert not getattr(search, "is_error", False), "ss_search_recordings over stdio returned isError"
    sr = _payload(search)
    assert "error" not in sr, f"ss_search_recordings over stdio: {describe_error(sr)}"
    assert sr.get("method") == "Search/SearchRecordings", "ss_search_recordings over stdio: wrong method"
    for label, res in (("ss_status", status), ("ss_search_recordings", search)):
        live.assert_no_leaks(_payload(res), f"{label} over stdio (structured)")
        live.assert_no_leaks(_texts(res), f"{label} over stdio (text)")
