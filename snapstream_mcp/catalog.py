"""The method catalog: every SnapStream API method with its parameters and access class.

Access classes:
  read     safe to call through ss_call_read and the curated tools
  write    changes state; only reachable through ss_stage_write -> ss_commit_write
  blocked  never callable (returns secrets, handles credentials, or is internal plumbing)

Anything unknown or malformed is treated as write (fail closed). Unknown methods are refused.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .formatting import is_guid, to_wcf_date

DATA_FILE = Path(__file__).resolve().parent / "data" / "api_catalog.json"
VALID_ACCESS = ("read", "write", "blocked")


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
    auth_ticket: str = "auto_resolved"

    @property
    def key(self) -> str:
        return f"{self.service}/{self.name}"

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
        return {
            "service": self.service,
            "method": self.name,
            "access": self.access,
            "summary": self.summary,
            "params": [f"{n}: {t}" for n, t in self.params],
            "returns": self.returns,
        }

    def full(self) -> Dict[str, Any]:
        out = {
            "service": self.service,
            "method": self.name,
            "summary": self.summary,
            "access": self.access,
            "access_reason": self.access_reason,
            "params": [{"name": n, "type": t} for n, t in self.params],
            "auth_ticket": "omitted automatically (resolved by the gateway)" if self.auth_ticket == "auto_resolved" else "not applicable",
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
        if self.note:
            out["note"] = self.note
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


class Catalog:
    def __init__(self, data: Dict[str, Any]) -> None:
        self.raw = data
        self.services: List[Dict[str, Any]] = list(data.get("services", []))
        self._methods: Dict[Tuple[str, str], MethodInfo] = {}
        self._lower: Dict[Tuple[str, str], Tuple[str, str]] = {}
        for m in data.get("methods", []):
            access = m.get("access")
            if access not in VALID_ACCESS:
                access = "write"  # fail closed on a missing or unknown value
            info = MethodInfo(
                service=m["service"],
                name=m["name"],
                summary=m.get("summary", ""),
                params=tuple((p["name"], p["type"]) for p in m.get("params", [])),
                perms=tuple(m.get("perms", [])),
                features=tuple(m.get("features", [])),
                returns=m.get("returns", ""),
                access=access,
                access_reason=m.get("access_reason", ""),
                note=m.get("note", ""),
                auth_ticket=m.get("auth_ticket", "auto_resolved"),
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

    def get(self, service: str, method: str) -> Optional[MethodInfo]:
        info = self._methods.get((service, method))
        if info is None:
            key = self._lower.get(((service or "").lower(), (method or "").lower()))
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
        svc = (service or "").lower()
        acc = (access or "").lower()
        out: List[MethodInfo] = []
        for m in self._methods.values():
            if svc and m.service.lower() != svc:
                continue
            if acc and m.access != acc:
                continue
            if words:
                hay = " ".join([m.service, m.name, m.summary, m.returns, " ".join(f"{n} {t}" for n, t in m.params)]).lower()
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
            hits = [m.key for m in self._methods.values() if m.service.lower() == service.lower()][:limit]
        return sorted(hits)[:limit]


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
    if typ == "string":
        if isinstance(value, (dict, list)):
            raise ValueError(f"parameter '{name}' must be a string")
        return str(value)
    return value  # complex types pass through unchanged


def coerce_params(info: MethodInfo, params: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Any], List[str]]:
    """Validate params against the catalog. Returns (body, notes). Raises ValueError on bad input.

    * authTicket keys are dropped (never sent).
    * Unknown parameter names are rejected with the list of valid names.
    * int, bool, Guid, Guid[] and string values are checked; DateTime accepts ISO 8601 and is
      converted to '/Date(ms)/'. Complex types pass through unchanged.
    """
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("params must be a JSON object keyed by parameter name")
    notes: List[str] = []
    types = info.param_types
    body: Dict[str, Any] = {}
    unknown = []
    for k, v in params.items():
        if str(k).lower() == "authticket":
            notes.append("authTicket removed: the gateway resolves it.")
            continue
        if k not in types:
            unknown.append(str(k))
            continue
        body[k] = _coerce_one(k, types[k], v)
    if unknown:
        valid = ", ".join(info.param_names) or "(none)"
        raise ValueError(f"unknown parameter(s) for {info.key}: {', '.join(unknown)}. Valid parameters: {valid}")
    return body, notes


def missing_note(info: MethodInfo, body: Dict[str, Any]) -> Optional[str]:
    missing = [n for n in info.param_names if n not in body]
    return ("not supplied (server default applies): " + ", ".join(missing)) if missing else None
