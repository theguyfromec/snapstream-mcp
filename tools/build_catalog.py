"""Regenerate snapstream_mcp/data/api_catalog.json from the SnapStream tenant API OpenAPI spec.

Usage:
    python tools/build_catalog.py PATH/TO/snapstream-tenant-api.json [--html PATH/TO/SnapStreamAPI.html]
        [--out PATH] [--overrides PATH] [--type-overrides PATH]
    python tools/build_catalog.py PATH/TO/SnapStreamAPI.html          (fallback: HTML reference only)

The OpenAPI JSON is the primary source. Each operation `POST /{Path}/json/{Op}` becomes one method:

* the service keeps its display name (Configuration, LocalConfiguration, NotifierManager; the
  others equal their path) and records `url_path`, the gateway path segment the client posts to;
* parameters, permissions (x-snapstream-permissions), features (x-snapstream-features), the
  read-only flag (x-snapstream-read-only), the return type, `valid_ticket_only` (the spec's
  "Guarded by ValidTicket alone" sentence) and `auth_ticket` come from the spec. `auth_ticket` is
  "empty_object" when the permission check depends on the input properties (the gateway inspector
  needs "authTicket": {} on builds before the fix), else "omit"; access_overrides.json can override it;
* the optional --html reference supplies service display names and descriptions, the shorter
  method summaries and the per-method notes;
* every method gets an `access` value (read, write or blocked) from tools/access_overrides.json.
  A method missing from that file is built as `write` (fail closed) and listed in the output. A
  read method that the spec flags as not read-only fails the build unless its override row has a
  `read_only_comment`. Override rows marked `not_in_spec` (FileAccess) are kept and must be blocked;
* a trimmed `schemas` section is built from components.schemas (properties with type and
  description, enum names, the four bit-flag enums parsed from their descriptions, __type hints),
  with tools/type_overrides.json merged over it.

Both documents are treated strictly as data: nothing in them is executed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DEFAULT_OUT = ROOT / "snapstream_mcp" / "data" / "api_catalog.json"
DEFAULT_OVERRIDES = HERE / "access_overrides.json"
DEFAULT_TYPE_OVERRIDES = HERE / "type_overrides.json"
VALID_ACCESS = ("read", "write", "blocked")
VALID_AUTH_TICKET = ("omit", "empty_object")
BS = chr(92)
EM_DASH = chr(0x2014)

# Gateway path segment -> catalog display name. Every other service uses its path as its name.
DISPLAY_NAMES = {"Config": "Configuration", "LocalConfig": "LocalConfiguration", "Notifier": "NotifierManager"}
URL_PATHS = {v: k for k, v in DISPLAY_NAMES.items()}
AUTH_TICKET_NOTE = "Permission requirement depends on the input properties; base permission(s) shown."
VALID_TICKET_SENTENCE = "Guarded by ValidTicket alone"
DESC_LIMIT = 220
DETAILS_LIMIT = 1200


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


def _trim(s: Any, limit: int = DESC_LIMIT) -> str:
    s = _clean_text(s or "")
    return s if len(s) <= limit else s[: limit - 3].rstrip() + "..."


# --------------------------------------------------------------------- overrides
def load_overrides(path: Path) -> Dict[tuple, Dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data["methods"] if isinstance(data, dict) else data
    out: Dict[tuple, Dict[str, Any]] = {}
    for r in rows:
        key = (r["service"], r["method"])
        if key in out:
            raise SystemExit(f"Duplicate override for {key[0]}/{key[1]}")
        if r.get("access") not in VALID_ACCESS:
            raise SystemExit(f"Invalid access value for {key[0]}/{key[1]}: must be one of {VALID_ACCESS}")
        if r.get("auth_ticket") is not None and r["auth_ticket"] not in VALID_AUTH_TICKET:
            raise SystemExit(f"Invalid auth_ticket value for {key[0]}/{key[1]}: must be one of {VALID_AUTH_TICKET}")
        if r.get("not_in_spec") and r["access"] != "blocked":
            raise SystemExit(f"{key[0]}/{key[1]} is not in the OpenAPI spec, so it must be blocked.")
        out[key] = {
            "access": r["access"],
            "reason": _clean_text(r.get("reason", "")),
            "read_only_comment": _clean_text(r.get("read_only_comment", "")),
            "auth_ticket": r.get("auth_ticket"),
            "ui_name": _clean_text(r.get("ui_name", "")),
            "not_in_spec": r.get("not_in_spec"),
        }
    return out


# ------------------------------------------------------------------ HTML (fallback)
def _html_services(html_path: Path) -> Tuple[List[Dict[str, Any]], bytes]:
    raw = html_path.read_bytes()
    src = raw.decode("utf-8", errors="replace")
    return _js_literal_to_json(_extract_services_literal(src)), raw


def build_from_html(html_path: Path, overrides_path: Path) -> Dict[str, Any]:
    """Fallback build from the HTML reference alone (no schemas, no spec flags)."""
    services, raw = _html_services(html_path)
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
                "url_path": URL_PATHS.get(path, path),
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
            params = [{"name": p[0], "type": p[1]} for p in m.get("p", []) if p[0] != "authTicket"]
            ov = overrides.get(key)
            if ov is None:
                access, reason = "write", "Not listed in tools/access_overrides.json; built as write (fail closed)."
                defaulted.append(f"{path}/{name}")
            else:
                access, reason = ov["access"], ov["reason"]
            note = _clean_text(m.get("note", ""))
            auth = (ov or {}).get("auth_ticket") or ("empty_object" if note == AUTH_TICKET_NOTE else "omit")
            entry: Dict[str, Any] = {
                "service": path,
                "name": name,
                "summary": _clean_text(m.get("s", "")),
                "params": params,
                "auth_ticket": auth,
                "perms": list(m.get("perms", [])),
                "features": list(m.get("feats", [])),
                "returns": _clean_return_type(m.get("ret", "")),
                "access": access,
                "access_reason": reason,
            }
            if note:
                entry["note"] = note
            if ov and ov.get("ui_name"):
                entry["ui_name"] = ov["ui_name"]
            methods.append(entry)
    stale = sorted(f"{s}/{n}" for (s, n) in overrides if (s, n) not in seen)
    return _envelope(
        {"html": {"file": html_path.name, "sha256": hashlib.sha256(raw).hexdigest()}},
        out_services, methods, defaulted, stale, {},
    )


# ------------------------------------------------------------------- OpenAPI types
def _ref_name(ref: str) -> str:
    return ref.rsplit("/", 1)[-1]


def _ancestors(name: str, schemas: Dict[str, Any]) -> List[str]:
    """name, its base, its base's base, ... (via allOf $ref)."""
    chain, cur = [], name
    while cur and cur not in chain:
        chain.append(cur)
        s = schemas.get(cur) or {}
        cur = next((_ref_name(x["$ref"]) for x in s.get("allOf", []) if isinstance(x, dict) and "$ref" in x), "")
    return chain


def _common_base(refs: List[str], schemas: Dict[str, Any]) -> str:
    """The nearest type that every ref in an anyOf list is or extends (the declared type)."""
    if not refs:
        return "object"
    chains = [_ancestors(r, schemas) for r in refs]
    for cand in chains[0]:
        if all(cand in c for c in chains):
            return cand
    return refs[0]


def spec_type(sch: Any, schemas: Dict[str, Any]) -> str:
    """OpenAPI schema -> the catalog's type name (Guid, int, long, bool, double, string, DateTime,
    TimeSpan, object, a schema name, or any of those with [])."""
    if not isinstance(sch, dict) or not sch:
        return "object"
    if "$ref" in sch:
        return _ref_name(sch["$ref"])
    if sch.get("anyOf"):
        return _common_base([_ref_name(x["$ref"]) for x in sch["anyOf"] if "$ref" in x], schemas)
    refs = [x for x in sch.get("allOf", []) if isinstance(x, dict) and "$ref" in x]
    if refs and not sch.get("properties"):
        return _ref_name(refs[0]["$ref"])
    t = sch.get("type")
    fmt = sch.get("format", "")
    if t == "array":
        return spec_type(sch.get("items"), schemas) + "[]"
    if t == "string":
        if fmt == "uuid":
            return "Guid"
        if "/Date" in str(sch.get("pattern", "")) or "/Date" in str(sch.get("example", "")):
            return "DateTime"
        if str(sch.get("example", "")).startswith("PT"):
            return "TimeSpan"
        return "string"
    if t == "integer":
        return "long" if fmt == "int64" else "int"
    if t == "number":
        return "double"
    if t == "boolean":
        return "bool"
    return "object"


_FLAG_RE = re.compile(r"(\d+)\s*=\s*([A-Za-z_]\w*)")


def _merged_props(name: str, schemas: Dict[str, Any]) -> Dict[str, Any]:
    s = schemas.get(name) or {}
    props: Dict[str, Any] = {}
    for x in s.get("allOf", []):
        if isinstance(x, dict) and "$ref" not in x:
            props.update(x.get("properties", {}) or {})
    props.update(s.get("properties", {}) or {})
    return props


def trim_schema(name: str, s: Dict[str, Any], schemas: Dict[str, Any]) -> Dict[str, Any]:
    """One components.schemas entry -> a compact catalog entry. Properties are the schema's own;
    inherited ones are reached through 'base'."""
    desc = (s.get("description") or "").split("\n\n")
    out: Dict[str, Any] = {"description": _trim(desc[0], 700)}
    if "enum" in s:
        names = s.get("x-enum-varnames") or [str(v) for v in s["enum"]]
        out["kind"] = "enum"
        out["values"] = {str(n): v for n, v in zip(names, s["enum"])}
        return out
    if s.get("type") == "integer" and "Bit flags" in (s.get("description") or ""):
        text = (s.get("description") or "").split("Bit flags", 1)[1]
        out["kind"] = "flags"
        out["values"] = {n: int(v) for v, n in _FLAG_RE.findall(text)}
        return out
    if s.get("type") in ("integer", "number", "string", "boolean") and not s.get("properties"):
        out["kind"] = "scalar"
        out["type"] = spec_type(s, schemas)
        return out
    out["kind"] = "object"
    base = next((_ref_name(x["$ref"]) for x in s.get("allOf", []) if isinstance(x, dict) and "$ref" in x), "")
    if base:
        out["base"] = base
    props: Dict[str, Any] = {}
    for pname, psch in _merged_props(name, schemas).items():
        if pname == "__type":
            enum = (psch or {}).get("enum") or []
            if enum:
                out["type_hint"] = enum[0]
            continue
        entry = {"type": spec_type(psch, schemas)}
        pdesc = _trim(((psch or {}).get("description") or "").split("\n\n")[0])
        if pdesc:
            entry["description"] = pdesc
        props[pname] = entry
    out["properties"] = props
    return out


def build_schemas(spec: Dict[str, Any], type_overrides: Dict[str, Any]) -> Dict[str, Any]:
    raw = (spec.get("components") or {}).get("schemas") or {}
    out = {name: trim_schema(name, s, raw) for name, s in sorted(raw.items())}
    for name, ov in (type_overrides.get("schemas") or {}).items():
        entry = out.setdefault(name, {"kind": "object", "description": "", "properties": {}})
        if ov.get("required"):
            entry["required"] = list(ov["required"])
        if ov.get("description_append"):
            entry["description"] = (entry.get("description", "") + " " + _clean_text(ov["description_append"])).strip()
        inherited = _all_props(name, out)
        for pname, pov in (ov.get("properties") or {}).items():
            cur = dict(entry["properties"].get(pname) or inherited.get(pname) or {})
            if pov.get("type"):
                cur["type"] = pov["type"]
            if "type" not in cur:
                raise SystemExit(f"type_overrides: {name}.{pname} is not in the spec, so it needs a 'type'.")
            if pov.get("status"):
                cur["status"] = pov["status"]
            if pov.get("description"):
                cur["description"] = (cur.get("description", "") + " " + _clean_text(pov["description"])).strip()
            entry["properties"][pname] = cur
    return out


def _all_props(name: str, schemas: Dict[str, Any]) -> Dict[str, Any]:
    props: Dict[str, Any] = {}
    seen = []
    cur = name
    chain = []
    while cur and cur in schemas and cur not in seen:
        seen.append(cur)
        chain.append(cur)
        cur = schemas[cur].get("base", "")
    for n in reversed(chain):
        props.update(schemas[n].get("properties") or {})
    return props


def _tick_fields(schemas: Dict[str, Any]) -> List[str]:
    """long fields that hold 100 ns ticks: *Position and *Ticks (not the uptime counters)."""
    names = set()
    for s in schemas.values():
        for pname, p in (s.get("properties") or {}).items():
            if p.get("type") == "long" and pname.endswith(("Position", "Ticks")) and "uptime" not in p.get("description", "").lower():
                names.add(pname)
    return sorted(names)


# ------------------------------------------------------------------ OpenAPI build
def _op_body_props(op: Dict[str, Any]) -> Dict[str, Any]:
    rb = op.get("requestBody") or {}
    sch = ((rb.get("content") or {}).get("application/json") or {}).get("schema") or {}
    return sch.get("properties") or {}


def _op_returns(op: Dict[str, Any], name: str, schemas: Dict[str, Any]) -> str:
    resp = ((op.get("responses") or {}).get("200") or {}).get("content") or {}
    sch = (resp.get("application/json") or {}).get("schema") or {}
    props = sch.get("properties") or {}
    if f"{name}Result" in props:
        return spec_type(props[f"{name}Result"], schemas)
    return "void"


def _depends_on_input(desc: str) -> bool:
    """True when the 'Requires ...' paragraph makes the permission conditional on the input."""
    for para in desc.split("\n\n"):
        if para.startswith("Requires"):
            head = para.split(". Also gated")[0]
            if re.search(r" when `", head):
                return True
    return False


def _details(desc: str) -> str:
    paras = [p for p in desc.split("\n\n")[1:] if p.strip()]
    return _trim(" ".join(paras), DETAILS_LIMIT)


def build(spec_path: Path, overrides_path: Path, html_path: Optional[Path] = None,
          type_overrides_path: Optional[Path] = DEFAULT_TYPE_OVERRIDES) -> Dict[str, Any]:
    raw = spec_path.read_bytes()
    spec = json.loads(raw.decode("utf-8"))
    schemas_raw = (spec.get("components") or {}).get("schemas") or {}
    overrides = load_overrides(overrides_path)
    type_ov = json.loads(type_overrides_path.read_text(encoding="utf-8")) if type_overrides_path and type_overrides_path.is_file() else {}

    html_svc: Dict[str, Dict[str, Any]] = {}
    html_m: Dict[tuple, Dict[str, Any]] = {}
    source: Dict[str, Any] = {
        "openapi": {
            "file": spec_path.name,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "title": (spec.get("info") or {}).get("title", ""),
            "version": (spec.get("info") or {}).get("version", ""),
        }
    }
    if html_path is not None:
        hs, hraw = _html_services(html_path)
        source["html"] = {"file": html_path.name, "sha256": hashlib.sha256(hraw).hexdigest()}
        for svc in hs:
            html_svc[svc["path"]] = svc
            for m in svc.get("methods", []):
                html_m[(svc["path"], m["n"])] = m

    tag_desc = {t["name"]: t.get("description", "") for t in spec.get("tags", [])}
    by_service: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []
    defaulted: List[str] = []
    ro_conflicts: List[str] = []
    seen = set()
    for path, item in spec.get("paths", {}).items():
        parts = path.strip("/").split("/")
        if len(parts) != 3 or parts[1] != "json" or "post" not in item:
            raise SystemExit(f"Unexpected path shape in the spec: {path}")
        url_path, _, name = parts
        service = DISPLAY_NAMES.get(url_path, url_path)
        op = item["post"]
        key = (service, name)
        seen.add(key)
        if service not in by_service:
            by_service[service] = []
            order.append(service)
        desc = op.get("description") or ""
        hm = html_m.get(key) or {}
        params = [
            {"name": n, "type": spec_type(s, schemas_raw)}
            for n, s in _op_body_props(op).items()
            if n.lower() != "authticket"
        ]
        ov = overrides.get(key)
        if ov is None:
            access, reason = "write", "Not listed in tools/access_overrides.json; built as write (fail closed)."
            defaulted.append(f"{service}/{name}")
        else:
            access, reason = ov["access"], ov["reason"]
        read_only = bool(op.get("x-snapstream-read-only", False))
        if access == "read" and not read_only and not (ov or {}).get("read_only_comment"):
            ro_conflicts.append(f"{service}/{name}")
        depends = _depends_on_input(desc)
        note = _clean_text(hm.get("note", "")) or (AUTH_TICKET_NOTE if depends else "")
        auth = (ov or {}).get("auth_ticket") or ("empty_object" if depends or note == AUTH_TICKET_NOTE else "omit")
        entry: Dict[str, Any] = {
            "service": service,
            "name": name,
            "summary": _clean_text(hm.get("s", "")) or _trim(desc.split("\n\n")[0]) or name,
            "params": params,
            "auth_ticket": auth,
            "perms": list(op.get("x-snapstream-permissions") or []),
            "features": list(op.get("x-snapstream-features") or []),
            # The HTML reference keeps the .NET names (uint, Dictionary<...>) the spec flattens.
            "returns": _clean_return_type(hm.get("ret", "")) or _op_returns(op, name, schemas_raw),
            "read_only": read_only,
            "access": access,
            "access_reason": reason,
        }
        if VALID_TICKET_SENTENCE in desc:
            entry["valid_ticket_only"] = True
        if note:
            entry["note"] = note
        details = _details(desc)
        if details:
            entry["details"] = details
        if ov and ov.get("ui_name"):
            entry["ui_name"] = ov["ui_name"]
        if ov and ov.get("read_only_comment") and not read_only:
            entry["read_only_comment"] = ov["read_only_comment"]
        by_service[service].append(entry)

    if ro_conflicts:
        raise SystemExit(
            "These read methods are flagged not read-only by the spec and have no read_only_comment in "
            "tools/access_overrides.json: " + ", ".join(ro_conflicts)
        )

    # Methods the spec leaves out but the overrides keep (blocked by rule).
    for (service, name), ov in overrides.items():
        if (service, name) in seen or not ov.get("not_in_spec"):
            continue
        extra = ov["not_in_spec"]
        seen.add((service, name))
        if service not in by_service:
            by_service[service] = []
            order.append(service)
        entry = {
                "service": service,
                "name": name,
                "summary": _clean_text(extra.get("summary", "")) or name,
                "params": [{"name": p["name"], "type": p["type"]} for p in extra.get("params", [])],
                "auth_ticket": "omit",
                "perms": list(extra.get("perms", [])),
                "features": list(extra.get("features", [])),
                "returns": extra.get("returns", "void"),
                "read_only": False,
                "access": "blocked",
                "access_reason": ov["reason"],
                "not_in_spec": True,
            }
        if extra.get("note"):
            entry["note"] = _clean_text(extra["note"])
        by_service[service].append(entry)

    out_services: List[Dict[str, Any]] = []
    methods: List[Dict[str, Any]] = []
    for service in order:
        url_path = URL_PATHS.get(service, service)
        hsvc = html_svc.get(service) or {}
        tdesc = tag_desc.get(url_path, "")
        iface = hsvc.get("iface") or (re.search(r"\b(I\w+Api)\b", tdesc).group(1) if re.search(r"\b(I\w+Api)\b", tdesc) else "")
        out_services.append(
            {
                "service": service,
                "url_path": url_path,
                "name": _clean_text(hsvc.get("name", service)),
                "interface": iface,
                "description": _clean_text(hsvc.get("desc", "")) or _clean_text(tdesc),
                "method_count": len(by_service[service]),
                "in_spec": any(not m.get("not_in_spec") for m in by_service[service]),
            }
        )
        methods.extend(by_service[service])

    stale = sorted(f"{s}/{n}" for (s, n) in overrides if (s, n) not in seen)
    schemas = build_schemas(spec, type_ov)
    return _envelope(source, out_services, methods, defaulted, stale, schemas)


def _envelope(source, out_services, methods, defaulted, stale, schemas) -> Dict[str, Any]:
    counts: Dict[str, int] = {a: 0 for a in VALID_ACCESS}
    for e in methods:
        counts[e["access"]] += 1
    out: Dict[str, Any] = {
        "schema_version": 2,
        "source": source,
        "auth_ticket_note": (
            "Over the gateway the SnapStream session comes from the bearer token's client_id claim, so "
            "every caller runs as the one mapped API user. A caller-supplied authTicket is always removed. "
            "Methods marked auth_ticket 'empty_object' (permission depends on the input properties) are "
            "sent with \"authTicket\": {} because the gateway inspector throws without it on builds before "
            "the fix (SNAPSTREAM_MCP_EMPTY_AUTH_TICKET, default on); every other method omits it."
        ),
        "counts": {"services": len(out_services), "methods": len(methods), **counts},
        "defaulted_to_write": defaulted,
        "stale_overrides": stale,
        "services": out_services,
        "methods": methods,
    }
    if schemas:
        out["tick_fields"] = _tick_fields(schemas)
        out["schemas"] = schemas
    return out


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("source", type=Path, help="OpenAPI JSON (primary) or SnapStreamAPI.html (fallback)")
    ap.add_argument("--html", type=Path, default=None, help="HTML reference for display names, summaries and notes")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--overrides", type=Path, default=DEFAULT_OVERRIDES)
    ap.add_argument("--type-overrides", type=Path, default=DEFAULT_TYPE_OVERRIDES)
    args = ap.parse_args(argv)

    if args.source.suffix.lower() == ".json":
        catalog = build(args.source, args.overrides, args.html, args.type_overrides)
    else:
        catalog = build_from_html(args.source, args.overrides)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(catalog, indent=1, ensure_ascii=False) + "\n"
    if EM_DASH in text:
        raise SystemExit("Generated catalog still contains an em dash; refusing to write it.")
    args.out.write_text(text, encoding="utf-8", newline="\n")

    c = catalog["counts"]
    print(f"Wrote {args.out}")
    print(f"{c['services']} services, {c['methods']} methods: read {c['read']}, write {c['write']}, blocked {c['blocked']}")
    if catalog.get("schemas"):
        print(f"{len(catalog['schemas'])} schemas")
    if catalog["defaulted_to_write"]:
        print(f"WARNING: {len(catalog['defaulted_to_write'])} method(s) missing from overrides, built as write:")
        for n in catalog["defaulted_to_write"]:
            print("  " + n)
    if catalog["stale_overrides"]:
        print(f"WARNING: {len(catalog['stale_overrides'])} override(s) match no method in the source:")
        for n in catalog["stale_overrides"]:
            print("  " + n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
