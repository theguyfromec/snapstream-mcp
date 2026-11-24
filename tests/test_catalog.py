import json
from pathlib import Path

import pytest

from snapstream_mcp.catalog import DATA_FILE, Catalog, coerce_params

ROOT = Path(__file__).resolve().parent.parent
OVERRIDES = ROOT / "tools" / "access_overrides.json"
EM_DASH = chr(0x2014)


def test_catalog_has_461_methods_each_with_access(catalog):
    assert len(catalog) == 461
    for m in catalog.all():
        assert m.access in ("read", "write", "blocked"), m.key
        assert m.access_reason, m.key


def test_counts_match_classification(catalog):
    c = catalog.counts()
    assert (c["services"], c["read"], c["write"], c["blocked"]) == (12, 246, 159, 56)


def test_raw_json_every_method_has_access_and_no_auth_ticket():
    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    assert len(data["methods"]) == 461
    assert data["defaulted_to_write"] == [] and data["stale_overrides"] == []
    for m in data["methods"]:
        assert m["access"] in ("read", "write", "blocked")
        assert all(p["name"] != "authTicket" for p in m["params"])
        assert m["auth_ticket"] in ("auto_resolved", "not_applicable")
        assert not m["returns"].startswith("//")


def test_catalog_matches_overrides(catalog):
    rows = json.loads(OVERRIDES.read_text(encoding="utf-8"))["methods"]
    assert len(rows) == 461
    for r in rows:
        assert catalog.get(r["service"], r["method"]).access == r["access"]


def test_no_em_dash_in_catalog():
    assert EM_DASH not in DATA_FILE.read_text(encoding="utf-8")


def test_key_classifications(catalog):
    assert catalog.access_of("Library", "GetMediaItems") == "read"
    assert catalog.access_of("Library", "DeleteMediaItem") == "write"
    assert catalog.access_of("AuthenticationManager", "Login") == "blocked"
    assert catalog.access_of("Scheduler", "GetRecordingRtmpInfo") == "blocked"
    assert catalog.access_of("StreamScheduler", "GetStreamLinkForChannel") == "blocked"  # URL may carry a stream credential
    # Same method name in two services is kept apart.
    assert catalog.access_of("Configuration", "SetSetting") == "write"
    assert catalog.access_of("LocalConfiguration", "GetSettingAsString") == "blocked"
    assert catalog.get("library", "getmediaitems").key == "Library/GetMediaItems"


def test_unknown_or_bad_access_fails_closed():
    cat = Catalog({"methods": [
        {"service": "X", "name": "A", "params": [], "access": "readonly"},
        {"service": "X", "name": "B", "params": []},
    ]})
    assert cat.access_of("X", "A") == "write"
    assert cat.access_of("X", "B") == "write"
    assert cat.access_of("X", "Missing") is None


def test_search_and_describe(catalog):
    hits = catalog.search("transcript", service="Library")
    assert any(h.name == "GetTranscript" for h in hits)
    assert all(h.access == "blocked" for h in catalog.search(access="blocked", limit=100))
    full = catalog.get("Library", "GetMediaItems").full()
    assert full["paged"] is True and full["example_params"] == {"index": 0, "count": 25}


def test_coerce_params(catalog):
    info = catalog.get("Scheduler", "GetAiringsByChannelAndTimeRange")
    body, notes = coerce_params(info, {
        "authTicket": "x", "index": "0", "count": 10,
        "channelId": "3FA85F64-5717-4562-B3FC-2C963F66AFA6",
        "rangeBegin": "1970-01-01T00:00:01Z", "rangeEnd": "/Date(2000)/",
    })
    assert body == {
        "index": 0, "count": 10, "channelId": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "rangeBegin": "/Date(1000)/", "rangeEnd": "/Date(2000)/",
    }
    assert any("authTicket" in n for n in notes)
    with pytest.raises(ValueError, match="unknown parameter"):
        coerce_params(info, {"chanelId": "x"})
    with pytest.raises(ValueError, match="GUID"):
        coerce_params(info, {"channelId": "not-a-guid"})
    with pytest.raises(ValueError, match="integer"):
        coerce_params(info, {"count": True})


SYNTH_HTML_TEMPLATE = """<html><body><script>
const OTHER = 1;
const SERVICES = [
  {id:"lib", name:'Library EMDASH test', iface:"ILibraryApi", path:"Library", desc:"Media items", methods:[
    {n:"GetThing", s:"Reads a thing.", p:[["authTicket","AuthTicket"],["id","Guid"]], perms:["ValidTicket"], ret:"Thing"},
    {n:"DoThing", s:'Changes "a" thing.', p:[["authTicket","AuthTicket"]], perms:[], ret:"// leaked comment Thing", note:"careful"}, // trailing comment
    {n:"Login", s:"Logs in.", p:[["username","string"],["password","string"]], perms:[], ret:"CreateSessionResult"},
  ]},
];
</script></body></html>"""
SYNTH_HTML = SYNTH_HTML_TEMPLATE.replace("EMDASH", EM_DASH)


def test_builder_fail_closed_and_auth_ticket(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("build_catalog", ROOT / "tools" / "build_catalog.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    html = tmp_path / "api.html"
    html.write_text(SYNTH_HTML, encoding="utf-8")
    ov = tmp_path / "ov.json"
    ov.write_text(json.dumps({"methods": [
        {"service": "Library", "method": "GetThing", "access": "read", "reason": "pure read"},
        {"service": "Library", "method": "Login", "access": "blocked", "reason": "credentials"},
        {"service": "Library", "method": "Gone", "access": "read", "reason": "stale"},
    ]}), encoding="utf-8")
    out = tmp_path / "cat.json"
    assert mod.main([str(html), "--out", str(out), "--overrides", str(ov)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    by = {m["name"]: m for m in data["methods"]}
    assert by["GetThing"]["access"] == "read"
    assert by["GetThing"]["params"] == [{"name": "id", "type": "Guid"}]
    assert by["GetThing"]["auth_ticket"] == "auto_resolved"
    assert by["DoThing"]["access"] == "write"  # missing from overrides: fail closed
    assert by["DoThing"]["returns"] == "Thing"
    assert by["Login"]["auth_ticket"] == "not_applicable" and by["Login"]["access"] == "blocked"
    assert data["defaulted_to_write"] == ["Library/DoThing"]
    assert data["stale_overrides"] == ["Library/Gone"]
    assert EM_DASH not in out.read_text(encoding="utf-8")
