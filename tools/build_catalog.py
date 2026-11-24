"""Regenerate snapstream_mcp/data/api_catalog.json from the SnapStream API HTML reference.

Usage:
    python tools/build_catalog.py PATH/TO/SnapStreamAPI.html [--out PATH] [--overrides PATH]

The HTML reference embeds its method catalog as a JavaScript literal (`const SERVICES = [...]`).
This script extracts that literal, converts it to JSON and writes a normalized catalog:

* every method keeps its service path, name, summary, parameters, permissions, features,
  return type and note;
* the `authTicket` parameter is removed from `params` (gateway calls must omit it) and the
  method records `"auth_ticket": "auto_resolved"` instead;
* every method gets an `access` value (read, write or blocked) from tools/access_overrides.json.
  A method missing from that file is built as `write` (fail closed) and listed in the output.

The HTML is treated strictly as data: nothing in it is executed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DEFAULT_OUT = ROOT / "snapstream_mcp" / "data" / "api_catalog.json"
DEFAULT_OVERRIDES = HERE / "access_overrides.json"
VALID_ACCESS = ("read", "write", "blocked")
BS = chr(92)
EM_DASH = chr(0x2014)


def _extract_services_literal(src: str) -> str:
    """Return the text of the `const SERVICES = [...]` array literal."""
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", src, flags=re.S)
    js = next((s for s in scripts if "const SERVICES = [" in s), None)
    if js is None:
        raise SystemExit("Could not find 'const SERVICES = [' in any <script> block of the HTML reference.")
    start = js.index("const SERVICES = [") + len("const SERVICES = ")
    i, depth, in_str, esc = start, 0, None, False
    while True:
        if i >= len(js):
            raise SystemExit("Unterminated SERVICES literal in the HTML reference.")
        c = js[i]
        if in_str:
            if esc:
                esc = False
            elif c == BS:
                esc = True
            elif c == in_str:
                in_str = None
        else:
            if c in "\"'":
                in_str = c
            elif c in "[{":
                depth += 1
            elif c in "]}":
                depth -= 1
                if depth == 0:
                    i += 1
                    break
        i += 1
    return js[start:i]


def _js_literal_to_json(lit: str) -> Any:
    """Convert a JavaScript object literal (unquoted keys, single quotes, comments) to Python data."""
    res: List[str] = []
    j, in_str, esc = 0, None, False
    while j < len(lit):
        c = lit[j]
        if in_str:
            res.append(c)
            if esc:
                esc = False
            elif c == BS:
                esc = True
            elif c == in_str:
                in_str = None
            j += 1
            continue
        if c == '"':
            in_str = '"'
            res.append(c)
            j += 1
            continue
        if c == "'":
            k = j + 1
            buf: List[str] = []
            while lit[k] != "'":
                if lit[k] == BS:
                    buf.append(lit[k:k + 2])
                    k += 2
                    continue
                buf.append(BS + '"' if lit[k] == '"' else lit[k])
                k += 1
            res.append('"' + "".join(buf) + '"')
            j = k + 1
            continue
        if lit[j:j + 2] == "//":
            nl = lit.find("\n", j)
            j = len(lit) if nl < 0 else nl
            continue
        m = re.match(r"([A-Za-z_]\w*)\s*:", lit[j:])
        if m and "".join(res).rstrip()[-1:] in ("{", ","):
            res.append('"%s":' % m.group(1))
            j += m.end()
            continue
        res.append(c)
        j += 1
    txt = re.sub(r",(\s*[\]}])", r"\1", "".join(res))
    return json.loads(txt)


def _clean_text(s: Any) -> Any:
    """House style: no em dashes in generated output."""
    if not isinstance(s, str):
        return s
    s = s.replace(" " + EM_DASH + " ", ", ").replace(EM_DASH, ", ")
    return re.sub(r"\s+", " ", s).strip()


def _clean_return_type(ret: str) -> str:
    """Some return types carry a leaked source comment, e.g. '// Ticket Permissions ... TaskList'."""
    ret = (ret or "").strip()
    if ret.startswith("//"):
        ret = ret.split()[-1]
    return ret


def load_overrides(path: Path) -> Dict[tuple, Dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data["methods"] if isinstance(data, dict) else data
    out: Dict[tuple, Dict[str, str]] = {}
    for r in rows:
        key = (r["service"], r["method"])
        if key in out:
            raise SystemExit(f"Duplicate override for {key[0]}/{key[1]}")
        if r.get("access") not in VALID_ACCESS:
            raise SystemExit(f"Invalid access value for {key[0]}/{key[1]}: must be one of {VALID_ACCESS}")
        out[key] = {"access": r["access"], "reason": _clean_text(r.get("reason", ""))}
    return out


def build(html_path: Path, overrides_path: Path) -> Dict[str, Any]:
    raw = html_path.read_bytes()
    src = raw.decode("utf-8", errors="replace")
    services = _js_literal_to_json(_extract_services_literal(src))
    overrides = load_overrides(overrides_path)

    out_services: List[Dict[str, Any]] = []
    methods: List[Dict[str, Any]] = []
    defaulted: List[str] = []
    seen = set()
    for svc in services:
        path = svc["path"]
        out_services.append(
            {
                "service": path,
                "name": _clean_text(svc.get("name", path)),
                "interface": svc.get("iface", ""),
                "description": _clean_text(svc.get("desc", "")),
                "method_count": len(svc.get("methods", [])),
            }
        )
        for m in svc.get("methods", []):
            name = m["n"]
            key = (path, name)
            seen.add(key)
            raw_params = m.get("p", [])
            had_ticket = any(p[0] == "authTicket" for p in raw_params)
            params = [{"name": p[0], "type": p[1]} for p in raw_params if p[0] != "authTicket"]
            ov = overrides.get(key)
            if ov is None:
                access = "write"
                reason = "Not listed in tools/access_overrides.json; built as write (fail closed)."
                defaulted.append(f"{path}/{name}")
            else:
                access, reason = ov["access"], ov["reason"]
            entry: Dict[str, Any] = {
                "service": path,
                "name": name,
                "summary": _clean_text(m.get("s", "")),
                "params": params,
                "auth_ticket": "auto_resolved" if had_ticket else "not_applicable",
                "perms": list(m.get("perms", [])),
                "features": list(m.get("feats", [])),
                "returns": _clean_return_type(m.get("ret", "")),
                "access": access,
                "access_reason": reason,
            }
            if m.get("note"):
                entry["note"] = _clean_text(m["note"])
            methods.append(entry)

    stale = sorted(f"{s}/{n}" for (s, n) in overrides if (s, n) not in seen)
    counts: Dict[str, int] = {a: 0 for a in VALID_ACCESS}
    for e in methods:
        counts[e["access"]] += 1
    return {
        "schema_version": 1,
        "source": {"file": html_path.name, "sha256": hashlib.sha256(raw).hexdigest()},
        "auth_ticket_note": (
            "Gateway calls resolve the AuthTicket from the SnapStream user mapped to the client_id. "
            "The authTicket parameter is removed from every method below and must be omitted from "
            "request bodies entirely (do not send null)."
        ),
        "counts": {"services": len(out_services), "methods": len(methods), **counts},
        "defaulted_to_write": defaulted,
        "stale_overrides": stale,
        "services": out_services,
        "methods": methods,
    }


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("html", type=Path, help="Path to SnapStreamAPI.html")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--overrides", type=Path, default=DEFAULT_OVERRIDES)
    args = ap.parse_args(argv)

    catalog = build(args.html, args.overrides)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(catalog, indent=1, ensure_ascii=False) + "\n"
    if EM_DASH in text:
        raise SystemExit("Generated catalog still contains an em dash; refusing to write it.")
    args.out.write_text(text, encoding="utf-8", newline="\n")

    c = catalog["counts"]
    print(f"Wrote {args.out}")
    print(f"{c['services']} services, {c['methods']} methods: read {c['read']}, write {c['write']}, blocked {c['blocked']}")
    if catalog["defaulted_to_write"]:
        print(f"WARNING: {len(catalog['defaulted_to_write'])} method(s) missing from overrides, built as write:")
        for n in catalog["defaulted_to_write"]:
            print("  " + n)
    if catalog["stale_overrides"]:
        print(f"WARNING: {len(catalog['stale_overrides'])} override(s) match no method in the HTML:")
        for n in catalog["stale_overrides"]:
            print("  " + n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
