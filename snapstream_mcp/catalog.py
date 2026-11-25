"""The method catalog: every SnapStream API method with its parameters and access class, plus a
trimmed copy of the OpenAPI component schemas.

Access classes:
  read     safe to call through ss_call_read and the curated tools
  write    changes state; only reachable through ss_stage_write -> ss_commit_write
  blocked  never callable (returns secrets, handles credentials, or is internal plumbing)

Anything unknown or malformed is treated as write (fail closed). Unknown methods are refused.

Services keep their display names (Configuration, LocalConfiguration, NotifierManager, ...) and
carry url_path, the gateway path segment (Config, LocalConfig, Notifier). Either name is accepted
wherever a service is passed in.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .formatting import TICKS_PER_SECOND, is_guid, seconds_to_clock, to_wcf_date

DATA_FILE = Path(__file__).resolve().parent / "data" / "api_catalog.json"
VALID_ACCESS = ("read", "write", "blocked")
VALID_AUTH_TICKET = ("omit", "empty_object")
TYPE_HINT_KEY = "__type"
MAX_NESTED_DEPTH = 4

# ClipPropertiesType values that act on an existing clip (no source positions needed) and the
# single-frame screenshot type.
EXISTING_CLIP_TYPES = (3, 8, 10)
CREATE_IMAGE_TYPE = 12
EXISTING_CLIP_METHODS = ("CreateGifFromExistingClip", "DesignExistingClip", "UpdateDesign", "BatchShareClip")


@dataclass(frozen=True)
class MethodInfo:
    service: str
    name: str
    summary: str
    params: Tuple[Tuple[str, str], ...]
    perms: Tuple[str, ...]
    features: Tuple[str, ...]
    returns: str
    access: str
    access_reason: str
    note: str = ""
    auth_ticket: str = "omit"
    url_path: str = ""
    read_only: Optional[bool] = None
    valid_ticket_only: bool = False
    details: str = ""
    ui_name: str = ""
    not_in_spec: bool = False

    @property
    def key(self) -> str:
        return f"{self.service}/{self.name}"

    @property
    def path(self) -> str:
        """The gateway path segment the client posts to."""
        return self.url_path or self.service

    @property
    def param_names(self) -> List[str]:
        return [p[0] for p in self.params]

    @property
    def param_types(self) -> Dict[str, str]:
        return dict(self.params)

    @property
    def paged(self) -> bool:
        names = self.param_names
        return "index" in names and "count" in names

    def brief(self) -> Dict[str, Any]:
        out = {
            "service": self.service,
            "method": self.name,
            "access": self.access,
            "summary": self.summary,
            "params": [f"{n}: {t}" for n, t in self.params],
            "returns": self.returns,
        }
        if self.ui_name:
            out["ui_name"] = self.ui_name
        return out

    def full(self) -> Dict[str, Any]:
        out = {
            "service": self.service,
            "url_path": self.path,
            "method": self.name,
            "summary": self.summary,
            "access": self.access,
            "access_reason": self.access_reason,
            "params": [{"name": n, "type": t} for n, t in self.params],
            "auth_ticket": (
                'sent as "authTicket": {} (permission depends on the input; the gateway needs the stub on '
                "older builds; SNAPSTREAM_MCP_EMPTY_AUTH_TICKET=off disables it). Any caller value is removed."
                if self.auth_ticket == "empty_object"
                else "omitted (the gateway resolves the session from the client_id). Any caller value is removed."
            ),
            "permissions": list(self.perms),
            "features": list(self.features),
            "returns": self.returns,
            "paged": self.paged,
            "example_params": example_params(self),
            "how_to_call": {
                "read": "ss_call_read(service, method, params)",
                "write": "ss_stage_write(service, method, params, reason), then ss_commit_write(token) after the user approves",
                "blocked": "Not callable through this server.",
            }[self.access],
        }
        if self.read_only is not None:
            out["spec_read_only"] = self.read_only
        if self.valid_ticket_only:
            out["valid_ticket_only"] = True
            out["valid_ticket_note"] = (
                "Guarded by ValidTicket alone: known gateway limitation on builds without the SCRUM-7181 fix, "
                "so the call can fail over the gateway."
            )
        if self.note:
            out["note"] = self.note
        if self.details:
            out["details"] = self.details
        if self.ui_name:
            out["ui_name"] = self.ui_name
        if self.not_in_spec:
            out["not_in_spec"] = True
        return out


_EXAMPLES = {
    "int": 0,
    "long": 0,
    "uint": 0,
    "bool": False,
    "string": "",
    "Guid": "00000000-0000-0000-0000-000000000000",
    "DateTime": "2026-01-01T00:00:00Z",
    "Guid[]": [],
    "string[]": [],
    "ColumnFilter[]": None,
    "ColumnSort[]": None,
}


def example_params(info: MethodInfo) -> Dict[str, Any]:
    ex: Dict[str, Any] = {}
    for n, t in info.params:
        if n == "count":
            ex[n] = 25
        elif t in _EXAMPLES:
            ex[n] = _EXAMPLES[t]
        else:
            ex[n] = f"<{t}>"
    return ex


def _bare(typ: str) -> Tuple[str, int]:
    """'TimeCodedTag[]' -> ('TimeCodedTag', 1)."""
    n = 0
    while typ.endswith("[]"):
        typ = typ[:-2]
        n += 1
    return typ, n


class Catalog:
    def __init__(self, data: Dict[str, Any]) -> None:
        self.raw = data
        self.services: List[Dict[str, Any]] = list(data.get("services", []))
        self.schemas: Dict[str, Dict[str, Any]] = dict(data.get("schemas") or {})
        self.tick_fields = frozenset(data.get("tick_fields") or ())
        # service alias (display name or url_path, lower case) -> display name
        self._svc_alias: Dict[str, str] = {}
        self._svc_path: Dict[str, str] = {}
        for s in self.services:
            name = s.get("service", "")
            path = s.get("url_path") or name
            self._svc_path[name] = path
            self._svc_alias[name.lower()] = name
            self._svc_alias[path.lower()] = name
        self._methods: Dict[Tuple[str, str], MethodInfo] = {}
        self._lower: Dict[Tuple[str, str], Tuple[str, str]] = {}
        for m in data.get("methods", []):
            access = m.get("access")
            if access not in VALID_ACCESS:
                access = "write"  # fail closed on a missing or unknown value
            auth = m.get("auth_ticket")
            if auth not in VALID_AUTH_TICKET:
                auth = "omit"
            svc = m["service"]
            self._svc_alias.setdefault(svc.lower(), svc)
            info = MethodInfo(
                service=svc,
                name=m["name"],
                summary=m.get("summary", ""),
                params=tuple((p["name"], p["type"]) for p in m.get("params", [])),
                perms=tuple(m.get("perms", [])),
                features=tuple(m.get("features", [])),
                returns=m.get("returns", ""),
                access=access,
                access_reason=m.get("access_reason", ""),
                note=m.get("note", ""),
                auth_ticket=auth,
                url_path=self._svc_path.get(svc, svc),
                read_only=m.get("read_only"),
                valid_ticket_only=bool(m.get("valid_ticket_only", False)),
                details=m.get("details", ""),
                ui_name=m.get("ui_name", ""),
                not_in_spec=bool(m.get("not_in_spec", False)),
            )
            key = (info.service, info.name)
            self._methods[key] = info
            self._lower[(info.service.lower(), info.name.lower())] = key

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Catalog":
        p = Path(path) if path else DATA_FILE
        return cls(json.loads(p.read_text(encoding="utf-8")))

    def __len__(self) -> int:
        return len(self._methods)

    def all(self) -> List[MethodInfo]:
        return list(self._methods.values())

    # ------------------------------------------------------------- services
    def resolve_service(self, service: str) -> Optional[str]:
        """Display name for a display name or url_path (case-insensitive); None when unknown."""
        return self._svc_alias.get((service or "").strip().lower())

    def url_path(self, service: str) -> str:
        name = self.resolve_service(service) or service
        return self._svc_path.get(name, name)

    def get(self, service: str, method: str) -> Optional[MethodInfo]:
        svc = self.resolve_service(service) or service
        info = self._methods.get((svc, method))
        if info is None:
            key = self._lower.get(((svc or "").lower(), (method or "").lower()))
            info = self._methods.get(key) if key else None
        return info

    def access_of(self, service: str, method: str) -> Optional[str]:
        info = self.get(service, method)
        return info.access if info else None

    def counts(self) -> Dict[str, Any]:
        by_access = {a: 0 for a in VALID_ACCESS}
        by_service: Dict[str, Dict[str, int]] = {}
        for m in self._methods.values():
            by_access[m.access] += 1
            svc = by_service.setdefault(m.service, {a: 0 for a in VALID_ACCESS})
            svc[m.access] += 1
        return {"methods": len(self._methods), "services": len(by_service), **by_access, "by_service": by_service}

    def search(self, query: str = "", service: str = "", access: str = "", limit: int = 50) -> List[MethodInfo]:
        words = [w.lower() for w in (query or "").split() if w]
        svc = (self.resolve_service(service) or service or "").lower()
        acc = (access or "").lower()
        out: List[MethodInfo] = []
        for m in self._methods.values():
            if svc and m.service.lower() != svc:
                continue
            if acc and m.access != acc:
                continue
            if words:
                hay = " ".join(
                    [m.service, m.path, m.name, m.summary, m.returns, m.ui_name, " ".join(f"{n} {t}" for n, t in m.params)]
                ).lower()
                if not all(w in hay for w in words):
                    continue
            out.append(m)
        out.sort(key=lambda m: (m.service, m.name))
        return out[: max(1, limit)]

    def suggest(self, service: str, method: str, limit: int = 5) -> List[str]:
        """Close method names, for an unknown-method error."""
        needle = (method or "").lower()
        hits = [m.key for m in self._methods.values() if needle and (needle in m.name.lower() or m.name.lower() in needle)]
        if not hits and service:
            svc = (self.resolve_service(service) or service).lower()
            hits = [m.key for m in self._methods.values() if m.service.lower() == svc][:limit]
        return sorted(hits)[:limit]

    # -------------------------------------------------------------- schemas
    def schema(self, name: str) -> Optional[Dict[str, Any]]:
        return self.schemas.get(name)

    def ancestors(self, name: str) -> List[str]:
        chain: List[str] = []
        cur = name
        while cur and cur in self.schemas and cur not in chain:
            chain.append(cur)
            cur = self.schemas[cur].get("base", "")
        return chain

    def fields(self, name: str) -> Dict[str, Dict[str, Any]]:
        """Every property of an object schema, inherited ones included (base first)."""
        out: Dict[str, Dict[str, Any]] = {}
        for n in reversed(self.ancestors(name)):
            out.update(self.schemas[n].get("properties") or {})
        return out

    def subtypes(self, name: str) -> List[str]:
        return sorted(
            n for n, s in self.schemas.items()
            if n != name and s.get("type_hint") and name in self.ancestors(n)
        )

    def enum_values(self, typ: str) -> Optional[Dict[str, int]]:
        s = self.schemas.get(_bare(typ)[0])
        if s and s.get("kind") in ("enum", "flags"):
            return dict(s.get("values") or {})
        return None

    def describe_type(self, typ: str) -> Optional[Dict[str, Any]]:
        """A complex or enum type resolved one level: its fields with type, description and, for
        enum-typed fields, the name -> value map. None for scalars and unknown names."""
        base, depth = _bare(typ)
        s = self.schemas.get(base)
        if not s:
            return None
        out: Dict[str, Any] = {"type": typ}
        if depth:
            out["list_of"] = base
        if s.get("description"):
            out["description"] = s["description"]
        kind = s.get("kind")
        if kind in ("enum", "flags"):
            out["kind"] = kind
            out["values"] = dict(s.get("values") or {})
            if kind == "flags":
                out["note"] = "Bit flags: send the integer sum of the members you want."
            return out
        if kind != "object":
            return out if kind else None
        out["kind"] = "object"
        if s.get("required"):
            out["required"] = list(s["required"])
        if s.get("type_hint"):
            out["type_hint"] = s["type_hint"]
        fields: Dict[str, Any] = {}
        for fname, f in self.fields(base).items():
            entry = {k: v for k, v in f.items() if k in ("type", "description", "status")}
            ev = self.enum_values(f.get("type", ""))
            if ev is not None:
                entry["values"] = ev
            fields[fname] = entry
        out["fields"] = fields
        subs = self.subtypes(base)
        if subs:
            out["subtypes"] = subs[:40]
            out["subtype_note"] = (
                'To send a subtype, put "__type": "<Subtype>:#SnapStream.Services" first in the object.'
            )
        out["field_names_are_case_sensitive"] = True
        return out

    # ----------------------------------------------------------- validation
    def validate_nested(self, typ: str, value: Any, path: str, depth: int = 0) -> List[str]:
        """Check the keys of a complex value against its schema. Returns problems (empty when
        fine): unknown keys, and case-only mismatches with a 'did you mean'. __type is allowed and
        selects the subtype whose fields are then allowed. Nothing is auto-corrected."""
        if value is None or depth > MAX_NESTED_DEPTH:
            return []
        base, lists = _bare(typ)
        if lists:
            if not isinstance(value, list):
                return []
            problems: List[str] = []
            elem = base + "[]" * (lists - 1)
            for i, v in enumerate(value):
                problems += self.validate_nested(elem, v, f"{path}[{i}]", depth + 1)
            return problems
        s = self.schemas.get(base)
        if not s or s.get("kind") != "object" or not isinstance(value, dict):
            return []
        target = base
        problems = []
        hint = value.get(TYPE_HINT_KEY)
        if hint is not None:
            sub = str(hint).split(":", 1)[0]
            if sub in self.schemas and base in self.ancestors(sub):
                target = sub
            elif sub != base:
                problems.append(f"{path}.__type {hint!r} is not {base} or one of its subtypes")
        allowed = self.fields(target)
        lower = {k.lower(): k for k in allowed}
        unknown: List[str] = []
        for k, v in value.items():
            if k == TYPE_HINT_KEY:
                continue
            if k in allowed:
                problems += self.validate_nested(allowed[k].get("type", ""), v, f"{path}.{k}", depth + 1)
            elif str(k).lower() in lower:
                problems.append(
                    f"{path}.{k} has the wrong case: did you mean {lower[str(k).lower()]}? "
                    "Field names are case-sensitive and the server silently drops a mismatched key."
                )
            else:
                unknown.append(str(k))
        if unknown:
            problems.append(
                f"{path} has unknown field(s) {', '.join(unknown)} for type {target}; "
                f"see ss_api_describe for the fields"
            )
        return problems


# ------------------------------------------------------------------ coercion
def _coerce_one(name: str, typ: str, value: Any) -> Any:
    if value is None:
        return None
    if typ in ("int", "long", "uint", "short", "byte"):
        if isinstance(value, bool):
            raise ValueError(f"parameter '{name}' must be an integer")
        try:
            if isinstance(value, float) and not value.is_integer():
                raise ValueError
            return int(value)
        except (TypeError, ValueError):
            raise ValueError(f"parameter '{name}' must be an integer") from None
    if typ == "double":
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ValueError(f"parameter '{name}' must be a number") from None
    if typ == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        raise ValueError(f"parameter '{name}' must be true or false")
    if typ == "Guid":
        if not is_guid(value):
            raise ValueError(f"parameter '{name}' must be a GUID like 3fa85f64-5717-4562-b3fc-2c963f66afa6")
        return value.lower()
    if typ == "Guid[]":
        if not isinstance(value, list) or not all(is_guid(v) for v in value):
            raise ValueError(f"parameter '{name}' must be a list of GUID strings")
        return [v.lower() for v in value]
    if typ == "DateTime":
        try:
            return to_wcf_date(value)
        except ValueError as exc:
            raise ValueError(f"parameter '{name}' must be an ISO 8601 date-time ({exc})") from None
    if typ in ("string", "String"):
        if isinstance(value, (dict, list)):
            raise ValueError(f"parameter '{name}' must be a string")
        return str(value)
    return value  # complex types pass through; ss_stage_write checks their keys (validate_nested)


def coerce_params(info: MethodInfo, params: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Any], List[str]]:
    """Validate params against the catalog. Returns (body, notes). Raises ValueError on bad input.

    * authTicket keys are dropped (the client adds the {} stub itself where a method needs it).
    * Unknown parameter names are rejected with the list of valid names (and a 'did you mean'
      for a case-only mismatch).
    * int, bool, Guid, Guid[] and string values are checked; DateTime accepts ISO 8601 and is
      converted to '/Date(ms)/'. Complex types pass through unchanged here.
    """
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("params must be a JSON object keyed by parameter name")
    notes: List[str] = []
    types = info.param_types
    lower = {n.lower(): n for n in types}
    body: Dict[str, Any] = {}
    unknown = []
    for k, v in params.items():
        if str(k).lower() == "authticket":
            notes.append("Caller authTicket removed: the gateway resolves the session.")
            continue
        if k not in types:
            hint = lower.get(str(k).lower())
            unknown.append(f"{k} (did you mean {hint}?)" if hint else str(k))
            continue
        body[k] = _coerce_one(k, types[k], v)
    if unknown:
        valid = ", ".join(info.param_names) or "(none)"
        raise ValueError(f"unknown parameter(s) for {info.key}: {', '.join(unknown)}. Valid parameters: {valid}")
    return body, notes


def missing_note(info: MethodInfo, body: Dict[str, Any]) -> Optional[str]:
    missing = [n for n in info.param_names if n not in body]
    return ("not supplied (server default applies): " + ", ".join(missing)) if missing else None


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def check_clip_positions(method: str, props: Dict[str, Any]) -> Tuple[Optional[str], List[str]]:
    """Validate the 100 ns tick positions of a ClipProperties payload.

    Returns (error, preview_lines). Clips need integer StartPosition < EndPosition and a span of at
    least 1 s. A single-frame screenshot (ClipPropertiesType 12) needs Start <= End. Methods and
    types that act on an existing clip may leave both positions out."""
    t = props.get("ClipPropertiesType")
    has = "StartPosition" in props or "EndPosition" in props
    if not has and (t in EXISTING_CLIP_TYPES or method in EXISTING_CLIP_METHODS):
        return None, []
    s, e = props.get("StartPosition"), props.get("EndPosition")
    hint = "positions are 100 ns ticks (seconds x 10,000,000); did you send milliseconds?"
    if not (_is_int(s) and _is_int(e)):
        return f"ClipProperties.StartPosition and EndPosition must both be integers; {hint}", []
    if s < 0:
        return f"ClipProperties.StartPosition must not be negative; {hint}", []
    if t == CREATE_IMAGE_TYPE:
        if e < s:
            return f"ClipProperties.EndPosition must not be before StartPosition; {hint}", []
    else:
        if e <= s:
            return f"ClipProperties.StartPosition must be less than EndPosition; {hint}", []
        if e - s < TICKS_PER_SECOND:
            return f"ClipProperties span is {(e - s) / TICKS_PER_SECOND:.4f} s, under the 1 s minimum; {hint}", []
    lines = [
        f"Clip in:     {seconds_to_clock(s / TICKS_PER_SECOND, fraction=True)}  (StartPosition {s} ticks)",
        f"Clip out:    {seconds_to_clock(e / TICKS_PER_SECOND, fraction=True)}  (EndPosition {e} ticks)",
        f"Clip length: {seconds_to_clock((e - s) / TICKS_PER_SECOND, fraction=True)}",
    ]
    return None, lines
