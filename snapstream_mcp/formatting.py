"""Response normalization for agent-friendly output.

* Unwraps the WCF "<Method>Result" envelope.
* Converts "/Date(ms[+-hhmm])/" to ISO 8601 UTC. The ms value is always UTC; the optional offset
  only records the server's local zone, so it does not shift the instant.
* Maps the .NET min date and the empty GUID (both mean "unset") to None. A min date of local or
  unspecified kind can arrive shifted by the server's UTC offset (up to 14 hours), so any instant
  within 14 hours of the min date counts as unset.
* Keeps ISO 8601 durations and adds a sibling "<key>_seconds" number.
* Optionally drops null and empty fields, and caps the serialized size with a paging hint.
* Converts agent-supplied ISO 8601 date-times to the "/Date(ms)/" form for requests.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from typing import Any, Dict, List, Optional, Tuple

DATE_RE = re.compile(r"^/Date\((-?\d+)([+-]\d{4})?\)/$")
DURATION_RE = re.compile(
    r"^(-)?P(?:(\d+(?:\.\d+)?)D)?(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?$"
)
GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
EMPTY_GUID = "00000000-0000-0000-0000-000000000000"
MIN_DATE_MS = -62135596800000
# DateTime.MinValue of kind Local or Unspecified serializes as UTC-shifted ticks plus an offset, for
# example "/Date(-62135575200000-0600)/". UTC offsets never exceed 14 hours.
UNSET_DATE_MAX_MS = MIN_DATE_MS + 14 * 3600 * 1000
_UTC = _dt.timezone.utc
_EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_UTC)


# ------------------------------------------------------------------ scalars
def wcf_date_to_iso(value: str) -> Optional[str]:
    """'/Date(1795552679779)/' -> '2026-11-24T...Z'. Min date and unparsable values -> None."""
    m = DATE_RE.match(value)
    if not m:
        return None
    ms = int(m.group(1))
    if ms <= UNSET_DATE_MAX_MS:
        return None
    try:
        dt = _EPOCH + _dt.timedelta(milliseconds=ms)
    except OverflowError:
        return None
    text = dt.isoformat(timespec="milliseconds" if ms % 1000 else "seconds")
    return text.replace("+00:00", "Z")


def iso_duration_seconds(value: str) -> Optional[float]:
    """'PT37M58.4304474S' -> 2278.4304474. Returns None when value is not an ISO 8601 duration."""
    m = DURATION_RE.match(value)
    if not m or value.endswith("T"):
        return None
    sign, d, h, mi, s = m.groups()
    if not any((d, h, mi, s)):
        return None
    total = float(d or 0) * 86400 + float(h or 0) * 3600 + float(mi or 0) * 60 + float(s or 0)
    total = round(total, 3)
    return -total if sign else total


def is_guid(value: Any) -> bool:
    return isinstance(value, str) and bool(GUID_RE.match(value))


def to_wcf_date(value: Any) -> Any:
    """Agent input -> '/Date(ms)/'. Accepts ISO 8601 (Z or offset; naive is taken as UTC), epoch ms,
    or an existing '/Date(...)/' string. Raises ValueError for anything else."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("expected an ISO 8601 date-time")
    if isinstance(value, (int, float)):
        return f"/Date({int(value)})/"
    if not isinstance(value, str):
        raise ValueError("expected an ISO 8601 date-time string")
    s = value.strip()
    if DATE_RE.match(s):
        return s
    if s.lower() == "now":
        dt = _dt.datetime.now(_UTC)
    else:
        try:
            dt = _dt.datetime.fromisoformat(s.replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError:
            raise ValueError(f"not an ISO 8601 date-time: {s[:40]!r}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_UTC)
    ms = int(round((dt - _EPOCH).total_seconds() * 1000))
    return f"/Date({ms})/"


def iso_now_plus(hours: float = 0.0) -> str:
    dt = _dt.datetime.now(_UTC) + _dt.timedelta(hours=hours)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


# --------------------------------------------------------------- structures
def unwrap_result(data: Any, method: Optional[str] = None) -> Any:
    """{'GetMediaItemsResult': [...]} -> [...]."""
    if isinstance(data, dict) and len(data) == 1:
        (key, val), = data.items()
        if (method and key == f"{method}Result") or (isinstance(key, str) and key.endswith("Result")):
            return val
    return data


def _norm_scalar(value: Any) -> Any:
    if isinstance(value, str):
        if value.startswith("/Date("):
            if DATE_RE.match(value):
                return wcf_date_to_iso(value)
            return value
        if value == EMPTY_GUID:
            return None
    return value


def normalize(data: Any) -> Any:
    """Recursively convert dates, unset GUIDs and durations (adds '<key>_seconds')."""
    if isinstance(data, dict):
        out: Dict[str, Any] = {}
        for k, v in data.items():
            nv = normalize(v)
            out[k] = nv
            if isinstance(v, str) and v.startswith(("P", "-P")):
                secs = iso_duration_seconds(v)
                if secs is not None and f"{k}_seconds" not in data:
                    out[f"{k}_seconds"] = secs
        return out
    if isinstance(data, list):
        return [normalize(v) for v in data]
    return _norm_scalar(data)


def compact(data: Any) -> Any:
    """Drop None, empty strings, empty lists and empty dicts. Keeps False and 0."""
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            cv = compact(v)
            if cv is None or cv == "" or cv == [] or cv == {}:
                continue
            out[k] = cv
        return out
    if isinstance(data, list):
        return [compact(v) for v in data]
    return data


def _size(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=False, default=str, separators=(",", ":")))


def _fit_list(items: List[Any], budget: int) -> int:
    """How many leading items fit in budget characters."""
    used, n = 2, 0
    for it in items:
        used += _size(it) + 1
        if used > budget:
            break
        n += 1
    return n


def cap_output(data: Any, max_chars: int, *, index: Optional[int] = None, paged: bool = False) -> Dict[str, Any]:
    """Wrap data as {'result': ...} and trim it to about max_chars of JSON.

    Lists keep as many leading items as fit. A dict keeps its fields and trims its largest list.
    Anything else that is too big becomes a text prefix. 'truncated' and 'hint' explain how to
    fetch the rest."""
    if _size(data) <= max_chars:
        return {"result": data, "truncated": False}
    page_hint = ""
    if paged:
        page_hint = " This method is paged: call it again with a smaller count, or with index advanced past the items returned."
    if isinstance(data, list):
        n = _fit_list(data, max_chars)
        nxt = (index or 0) + n
        return {
            "result": data[:n],
            "truncated": True,
            "items_returned": n,
            "items_in_response": len(data),
            "hint": f"Output capped at about {max_chars} characters; {len(data) - n} item(s) omitted."
            + (f" Next page: index={nxt}." if paged else "")
            + page_hint,
        }
    if isinstance(data, dict):
        lists = [(k, v) for k, v in data.items() if isinstance(v, list) and v]
        if lists:
            key, big = max(lists, key=lambda kv: _size(kv[1]))
            rest = {k: v for k, v in data.items() if k != key}
            budget = max_chars - _size(rest) - len(key) - 10
            if budget > 0:
                n = _fit_list(big, budget)
                trimmed = {**rest, key: big[:n]}
                return {
                    "result": trimmed,
                    "truncated": True,
                    "trimmed_field": key,
                    "items_returned": n,
                    "items_in_response": len(big),
                    "hint": f"Output capped at about {max_chars} characters; '{key}' trimmed to {n} of {len(big)} item(s)."
                    + page_hint,
                }
    text = json.dumps(data, ensure_ascii=False, default=str)
    return {
        "result_text": text[:max_chars],
        "truncated": True,
        "hint": f"Output capped at {max_chars} of {len(text)} characters." + page_hint,
    }


SECRET_FIELD_RE = re.compile(
    r"(?i)(passw|passwd|passphrase|secret|token|api_?key|access_?key|private_?key|credential|connection_?string|auth_?code|session_?key)"
)
# Stricter matching for responses whose schema is undocumented and may hold destination or
# device credentials (export destinations, task actions, external tuner properties).
STRICT_SECRET_FIELD_RE = re.compile(r"(?i)(pass|secret|token|key|credential|user|login|account|auth|cert|sas|signature)")
STRICT_REDACT_METHODS = re.compile(r"(?i)(ExportDestination|TaskAction|TunerPartProperties|ExternalTunerProperties)")
# Write methods whose parameters may carry destination or account credentials (AccountKey,
# StreamKey, UserName and the like). Their previews and audit lines use strict masking.
STRICT_REDACT_WRITE_METHODS = re.compile(
    r"(?i)(ExportDestination|TaskAction|TunerPartProperties|ExternalTunerProperties|^StreamTo\w*Live$|^Federated)"
)
REDACTED_FIELD = "[REDACTED]"


def redact_fields(data: Any, strict: bool = False) -> Any:
    """Mask values of credential-looking keys anywhere in a structure. Booleans and nulls are kept
    (a flag such as HasPassword reveals nothing); every other value is replaced."""
    pat = STRICT_SECRET_FIELD_RE if strict else SECRET_FIELD_RE
    if isinstance(data, dict):
        out: Dict[str, Any] = {}
        for k, v in data.items():
            if isinstance(k, str) and pat.search(k) and not isinstance(v, bool) and v is not None and v != "":
                kl = k.lower()
                if strict and not pat.search(kl.replace("user", "").replace("account", "")) and (
                    (kl.endswith("id") and (is_guid(v) or isinstance(v, int)))
                    or (kl.endswith(("count", "type")) and isinstance(v, (int, float)))
                ):
                    out[k] = v  # identifiers such as UserId or AccountType are not credentials
                else:
                    out[k] = REDACTED_FIELD
            else:
                out[k] = redact_fields(v, strict)
        return out
    if isinstance(data, list):
        return [redact_fields(v, strict) for v in data]
    return data


def shape(obj: Any, depth: int = 0, max_depth: int = 3) -> Any:
    """Describe the structure of obj (types and keys) without exposing any values."""
    if isinstance(obj, dict):
        if depth >= max_depth:
            return f"dict[{len(obj)} keys]"
        return {k: shape(v, depth + 1, max_depth) for k, v in list(obj.items())[:40]}
    if isinstance(obj, list):
        if not obj:
            return "list[0]"
        return [f"list[{len(obj)}] of", shape(obj[0], depth + 1, max_depth)]
    if obj is None:
        return "null"
    return type(obj).__name__


def format_response(
    raw: Any,
    method: str,
    *,
    max_chars: int,
    drop_empty: bool = True,
    index: Optional[int] = None,
    paged: bool = False,
) -> Dict[str, Any]:
    data = normalize(unwrap_result(raw, method))
    data = redact_fields(data, strict=bool(STRICT_REDACT_METHODS.search(method)))
    if drop_empty:
        data = compact(data)
    return cap_output(data, max_chars, index=index, paged=paged)


def seconds_to_clock(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


EPOCH_MS_MIN = 100_000_000_000  # 1e11 ms is March 1973; larger numbers are epoch milliseconds
EPOCH_S_MIN = 1_000_000_000  # 1e9 s is September 2001; no caption offset is that long


def _numeric_time(value: float, key: str) -> Tuple[Optional[float], str]:
    """Number -> (seconds, kind). The unit comes from the field name when it says so (StartMs,
    StartMilliseconds, StartSeconds); otherwise from the magnitude."""
    v = float(value)
    k = key.lower()
    if abs(v) >= EPOCH_MS_MIN:
        return v / 1000.0, "epoch_s"
    if k.endswith(("ms", "millis", "milliseconds")):
        return v / 1000.0, "offset_s"
    if k.endswith(("seconds", "secs", "sec")):
        return (v, "epoch_s") if abs(v) >= EPOCH_S_MIN else (v, "offset_s")
    if abs(v) >= EPOCH_S_MIN:
        return v, "epoch_s"
    # Unit unknown: large values are taken as milliseconds (over about 2.8 hours as seconds).
    return (v / 1000.0, "offset_s") if abs(v) > 10_000_000 else (v, "offset_s")


def parse_time_value(value: Any, key: str = "") -> Tuple[Optional[float], str]:
    """Interpret a caption time value. Returns (number, kind) where kind is 'epoch_s' for absolute
    instants, 'offset_s' for offsets in seconds, or '' when unknown. key is the field name; a
    numeric value takes its unit from it when the name ends in ms/milliseconds or seconds."""
    if isinstance(value, bool) or value is None:
        return None, ""
    if isinstance(value, (int, float)):
        return _numeric_time(value, key or "")
    if isinstance(value, str):
        m = DATE_RE.match(value)
        if m:
            ms = int(m.group(1))
            return (None, "") if ms <= UNSET_DATE_MAX_MS else (ms / 1000.0, "epoch_s")
        d = iso_duration_seconds(value)
        if d is not None:
            return d, "offset_s"
        tm = re.match(r"^(?:(\d+)\.)?(\d{1,2}):(\d{2}):(\d{2}(?:\.\d+)?)$", value)
        if tm:
            days, h, mi, s = tm.groups()
            return int(days or 0) * 86400 + int(h) * 3600 + int(mi) * 60 + float(s), "offset_s"
        try:
            dt = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_UTC)
            return (dt - _EPOCH).total_seconds(), "epoch_s"
        except ValueError:
            return None, ""
    return None, ""
