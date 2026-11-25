import json
from pathlib import Path

import pytest

from snapstream_mcp.catalog import DATA_FILE, Catalog, coerce_params

ROOT = Path(__file__).resolve().parent.parent
OVERRIDES = ROOT / "tools" / "access_overrides.json"
EM_DASH = chr(0x2014)


def test_catalog_has_479_methods_each_with_access(catalog):
    # 473 OpenAPI operations plus the 6 FileAccess methods the spec leaves out (kept as blocked).
    assert len(catalog) == 479
    for m in catalog.all():
        assert m.access in ("read", "write", "blocked"), m.key
        assert m.access_reason, m.key


def test_counts_match_classification(catalog):
    c = catalog.counts()
    assert (c["services"], c["read"], c["write"], c["blocked"]) == (12, 249, 162, 68)


def test_raw_json_every_method_has_access_and_no_auth_ticket():
    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    assert len(data["methods"]) == 479
    assert data["defaulted_to_write"] == [] and data["stale_overrides"] == []
    assert "openapi" in data["source"]
    for m in data["methods"]:
        assert m["access"] in ("read", "write", "blocked")
        assert all(p["name"].lower() != "authticket" for p in m["params"])
        assert m["auth_ticket"] in ("omit", "empty_object")
        assert not m["returns"].startswith("//")
        if m["access"] == "read":
            assert m["read_only"] or m.get("read_only_comment"), m["name"]


def test_catalog_matches_overrides(catalog):
    rows = json.loads(OVERRIDES.read_text(encoding="utf-8"))["methods"]
    assert len(rows) == 479
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


def _builder():
    import importlib.util

    spec = importlib.util.spec_from_file_location("build_catalog", ROOT / "tools" / "build_catalog.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_builder_html_fallback_fail_closed(tmp_path):
    mod = _builder()

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
    assert by["GetThing"]["auth_ticket"] == "omit"
    assert by["DoThing"]["access"] == "write"  # missing from overrides: fail closed
    assert by["DoThing"]["returns"] == "Thing"
    assert by["Login"]["auth_ticket"] == "omit" and by["Login"]["access"] == "blocked"
    assert data["defaulted_to_write"] == ["Library/DoThing"]
    assert data["stale_overrides"] == ["Library/Gone"]
    assert EM_DASH not in out.read_text(encoding="utf-8")


# --------------------------------------------------------------- OpenAPI build
from snapstream_mcp.catalog import check_clip_positions  # noqa: E402

EMPTY_OBJECT_METHODS = {
    "ClipFile", "CreateScreenshot", "ClipAndTweet", "ClipAndFacebookPost", "ClipAndUpload", "ClipAndSlack",
    "ClipAndSlackGIF", "UploadClip", "BatchUploadClipsToYouTube", "TweetClip", "TweetClipAsGIF", "TweetScreenshot",
    "TweetExistingGIF", "PostClipToFacebook", "SlackClip", "SlackClipAsGIF", "SlackExistingGIF", "SlackImage",
    "PostExistingMoment",
}


def test_service_url_paths_and_aliases(catalog):
    assert catalog.get("Config", "GetApiLevel").key == "Configuration/GetApiLevel"
    assert catalog.get("Configuration", "GetApiLevel").path == "Config"
    assert catalog.get("localconfig", "getapilevel").path == "LocalConfig"
    assert catalog.get("Notifier", "GetMyAlerts").key == "NotifierManager/GetMyAlerts"
    assert catalog.get("NotifierManager", "GetMyAlerts").path == "Notifier"
    assert catalog.get("Library", "GetMediaItems").path == "Library"
    assert catalog.resolve_service("CONFIG") == "Configuration"
    assert catalog.resolve_service("Nope") is None
    assert {m.service for m in catalog.search(service="Notifier", limit=100)} == {"NotifierManager"}
    paths = {s["service"]: s["url_path"] for s in catalog.services}
    assert paths["Configuration"] == "Config" and paths["LocalConfiguration"] == "LocalConfig"
    assert paths["NotifierManager"] == "Notifier" and paths["TaskManager"] == "TaskManager"


def test_spec_only_operations_are_classified(catalog):
    expected = {
        ("AuthenticationManager", "GenerateApiLoginCredentials"): "blocked",
        ("AuthenticationManager", "GenerateTVBoxLoginCredentials"): "blocked",
        ("Scheduler", "TestLiveFeedConnection"): "blocked",
        ("Scheduler", "SaveLiveFeedSelection"): "write",
        ("Scheduler", "CancelLiveFeedSelection"): "write",
        ("Scheduler", "DeleteLiveFeedSelection"): "write",
        ("Scheduler", "GetLiveFeedDisplayChannel"): "write",
    }
    for name in ("GetLiveFeedEvent", "GetLiveFeedEventItemCount", "GetLiveFeedEventItems", "GetLiveFeedEventRange",
                 "GetLiveFeedEvents", "GetLiveFeedProviderStatus", "GetLiveFeedRenditions", "GetLiveFeedSelections",
                 "GetLiveFeedSubscriptions", "GetLiveFeedUsage", "IsLiveFeedEnabled"):
        expected[("Scheduler", name)] = "read"
    assert len(expected) == 18
    for (svc, name), access in expected.items():
        assert catalog.access_of(svc, name) == access, name
    fa = [m for m in catalog.all() if m.service == "FileAccess"]
    assert len(fa) == 6
    for m in fa:
        assert m.access == "blocked" and m.not_in_spec
        assert "not exposed by the gateway per the openapi spec" in m.access_reason.lower()


def test_per_method_spec_flags(catalog):
    flagged = {m.name for m in catalog.all() if m.auth_ticket == "empty_object"}
    assert flagged == EMPTY_OBJECT_METHODS
    assert catalog.get("TaskManager", "AiClipDesigner").auth_ticket == "omit"
    assert catalog.get("Library", "GetMediaItems").auth_ticket == "omit"
    sc = catalog.get("Library", "GetSmartChapters")
    assert sc.valid_ticket_only and sc.read_only is True and sc.perms == ("ValidTicket",)
    assert "valid_ticket_note" in sc.full()
    assert not catalog.get("TaskManager", "ClipFile").valid_ticket_only
    ub = catalog.get("Configuration", "UseBackendMachineName")
    assert ub.access == "read" and ub.read_only is False
    assert catalog.get("TaskManager", "AiClipDesigner").ui_name == "Create for Social"
    assert any(m.name == "AiClipDesigner" for m in catalog.search("create for social"))
    full = catalog.get("TaskManager", "ClipFile").full()
    assert full["url_path"] == "TaskManager" and "authTicket" in full["auth_ticket"]
    assert "CreateClip" in full["details"]


def test_schemas_enums_flags_and_overrides(catalog):
    assert len(catalog.schemas) == 333
    cc = catalog.enum_values("CCSearchTypes")
    assert cc["TextDocDescription"] == 1 and cc["Title"] == 2 and cc["Text"] == 4 and cc["ProgramData"] == 58
    assert cc["Default"] == 254 and cc["All"] == 255 and cc["AIChapters"] == 128
    assert catalog.enum_values("EpisodeSearchTypes") == {"None": 0, "Description": 1, "Title": 2, "People": 4, "All": 3}
    assert catalog.enum_values("FolderPermission")["Full"] == 4294967295
    assert catalog.enum_values("FolderRoles")["All"] == 15
    assert catalog.enum_values("AiClipOrientation") == {"Vertical": 0, "Horizontal": 1, "Square": 2}
    assert catalog.enum_values("AiClipLayout") == {"Auto": 0, "Fill": 1, "Fit": 2, "Split": 3}
    assert catalog.schemas["CCSearchTypes"]["kind"] == "flags"
    assert catalog.schemas["ExportAkamai"]["type_hint"] == "ExportAkamai:#SnapStream.Services"
    cp = catalog.schemas["ClipProperties"]
    assert cp["required"] == ["ClipPropertiesType", "SourcePathId", "SourceFile", "StartPosition", "EndPosition"]
    fields = catalog.fields("ClipProperties")
    assert fields["StartPosition"]["type"] == "long" and fields["StartPosition"]["status"] == "required"
    assert fields["ItemId"]["type"] == "Guid"  # from tools/type_overrides.json, not in the spec
    assert fields["Subtitles"]["type"] == "bool" and "Capital S" in fields["Subtitles"]["description"]
    assert "UNRESOLVED" in fields["Orientation"]["description"]
    assert catalog.tick_fields == {
        "StartPosition", "EndPosition", "StartTicks", "EndTicks", "NewStartTicks", "NewEndTicks", "TimeoutTicks"}


def test_describe_type_resolves_one_level(catalog):
    d = catalog.describe_type("ClipProperties")
    assert d["kind"] == "object" and d["field_names_are_case_sensitive"] is True
    assert d["fields"]["Orientation"]["values"] == {"Vertical": 0, "Horizontal": 1, "Square": 2}
    assert d["fields"]["ClipPropertiesType"]["values"]["Clip"] == 1
    assert d["fields"]["SourcePathId"]["type"] == "Guid"
    assert "ClipTitle" in d["fields"]  # inherited from BaseClipProperties
    assert catalog.describe_type("CCSearchTypes")["kind"] == "flags"
    lst = catalog.describe_type("TimeCodedTag[]")
    assert lst["list_of"] == "TimeCodedTag" and "StartTicks" in lst["fields"]
    assert "ExportS3" in catalog.describe_type("ExportDestination")["subtypes"]
    assert catalog.describe_type("Guid") is None and catalog.describe_type("int") is None


def test_validate_nested(catalog):
    ok = {"ClipPropertiesType": 1, "Subtitles": True, "ClipTimeCodedTags": [{"Name": "a", "StartTicks": 0}]}
    assert catalog.validate_nested("ClipProperties", ok, "clipProperties") == []
    bad = catalog.validate_nested("ClipProperties", {"subtitles": True}, "clipProperties")
    assert len(bad) == 1 and "did you mean Subtitles" in bad[0]
    bad = catalog.validate_nested("ClipProperties", {"Subtitlez": True, "Bogus": 1}, "clipProperties")
    assert "Subtitlez" in bad[0] and "Bogus" in bad[0] and "unknown field" in bad[0]
    nested = catalog.validate_nested("ClipProperties", {"ClipTimeCodedTags": [{"name": "a"}]}, "cp")
    assert nested and "cp.ClipTimeCodedTags[0].name" in nested[0] and "did you mean Name" in nested[0]
    # __type selects a subtype whose own fields are then allowed.
    sub = {"__type": "ExportFtp:#SnapStream.Services", "FriendlyName": "x", "Username": "u"}
    assert catalog.validate_nested("ExportDestination", sub, "path") == []
    assert catalog.validate_nested("ExportDestination", {"Username": "u"}, "path")
    assert catalog.validate_nested("ExportDestination", {"__type": "Bogus:#SnapStream.Services"}, "path")
    assert catalog.validate_nested("Guid", "x", "id") == []


def test_top_level_param_case_hint(catalog):
    info = catalog.get("Library", "GetMediaItem")
    with pytest.raises(ValueError, match="did you mean itemId"):
        coerce_params(info, {"ItemId": "3fa85f64-5717-4562-b3fc-2c963f66afa6"})


@pytest.mark.parametrize("props,error", [
    ({"ClipPropertiesType": 1, "StartPosition": 62000, "EndPosition": 65000}, "did you send milliseconds"),
    ({"ClipPropertiesType": 1, "StartPosition": 62.5, "EndPosition": 650000000}, "integers"),
    ({"ClipPropertiesType": 1, "StartPosition": 650000000, "EndPosition": 620000000}, "less than EndPosition"),
    ({"ClipPropertiesType": 1}, "integers"),
    ({"ClipPropertiesType": 1, "StartPosition": -1, "EndPosition": 650000000}, "negative"),
])
def test_clip_positions_refused(props, error):
    err, lines = check_clip_positions("ClipFile", props)
    assert err and error in err and "100 ns ticks" in err and lines == []


def test_clip_positions_accepted():
    err, lines = check_clip_positions("ClipFile", {"ClipPropertiesType": 1, "StartPosition": 620_000_000, "EndPosition": 755_000_000})
    assert err is None
    assert "00:01:02" in lines[0] and "00:01:15.500" in lines[1] and "00:00:13.500" in lines[2]
    assert check_clip_positions("CreateScreenshot", {"ClipPropertiesType": 12, "StartPosition": 5, "EndPosition": 5})[0] is None
    assert check_clip_positions("DesignExistingClip", {"ClipPropertiesType": 1})[0] is None
    assert check_clip_positions("EmailClip", {"ClipPropertiesType": 8})[0] is None


SYNTH_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Synthetic", "version": "1"},
    "tags": [{"name": "Config", "description": "Generated from IConfigurationApi."}, {"name": "TaskManager", "description": "x"}],
    "paths": {
        "/Config/json/GetApiLevel": {"post": {
            "description": "Gets the API level.\n\nRequires `ValidTicket`.\n\nGuarded by ValidTicket alone, so it fails.",
            "x-snapstream-permissions": ["ValidTicket"], "x-snapstream-read-only": True,
            "requestBody": {"content": {"application/json": {"schema": {"type": "object", "properties": {
                "apiType": {"allOf": [{"$ref": "#/components/schemas/ApiType"}]}}}}}},
            "responses": {"200": {"content": {"application/json": {"schema": {"type": "object", "properties": {
                "GetApiLevelResult": {"type": "string"}}}}}}},
        }},
        "/TaskManager/json/ClipFile": {"post": {
            "description": "Creates a clip.\n\nRequires `CreateClip` when `clipProperties.ClipPropertiesType` is 1 (Clip).",
            "x-snapstream-permissions": ["CreateClip"], "x-snapstream-read-only": False,
            "requestBody": {"content": {"application/json": {"schema": {"type": "object", "properties": {
                "clipProperties": {"type": "object", "nullable": True, "allOf": [{"$ref": "#/components/schemas/Clip"}]},
                "when": {"type": "string", "pattern": "^/Date\\(-?\\d+([+-]\\d{4})?\\)/$"},
                "ids": {"type": "array", "items": {"type": "string", "format": "uuid"}}}}}}},
            "responses": {"200": {"content": {"application/json": {"schema": {"type": "object", "properties": {
                "ClipFileResult": {"type": "string", "format": "uuid"}}}}}}},
        }},
        "/TaskManager/json/TaskRunning": {"post": {
            "description": "Is a task running.", "x-snapstream-read-only": False, "responses": {}}},
        "/TaskManager/json/NewThing": {"post": {"description": "Does a new thing.", "responses": {}}},
    },
    "components": {"schemas": {
        "ApiType": {"type": "integer", "format": "int32", "description": "Api type", "enum": [0, 1], "x-enum-varnames": ["A", "B"]},
        "Flags": {"type": "integer", "format": "int32",
                  "description": "F.\n\nBit flags, serialized as the numeric sum of the members set: 1 = One, 2 = Two, 3 = Both."},
        "Base": {"type": "object", "properties": {"StartPosition": {"type": "integer", "format": "int64", "description": "ticks"}}},
        "Clip": {"allOf": [{"$ref": "#/components/schemas/Base"}, {"type": "object", "properties": {
            "__type": {"type": "string", "enum": ["Clip:#SnapStream.Services"]},
            "Title": {"type": "string", "description": "The title."}}}]},
    }},
}


def _write_overrides(path, rows):
    path.write_text(json.dumps({"methods": rows}), encoding="utf-8")


def test_builder_openapi(tmp_path):
    mod = _builder()
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(SYNTH_SPEC), encoding="utf-8")
    ov = tmp_path / "ov.json"
    _write_overrides(ov, [
        {"service": "Configuration", "method": "GetApiLevel", "access": "read", "reason": "pure read"},
        {"service": "TaskManager", "method": "ClipFile", "access": "write", "reason": "renders"},
        {"service": "TaskManager", "method": "TaskRunning", "access": "read", "reason": "query",
         "read_only_comment": "spec flag is wrong"},
        {"service": "FileAccess", "method": "Exists", "access": "blocked", "reason": "Not exposed by the gateway per the OpenAPI spec.",
         "not_in_spec": {"summary": "Exists.", "params": [{"name": "path", "type": "string"}], "returns": "bool"}},
    ])
    tov = tmp_path / "types.json"
    tov.write_text(json.dumps({"schemas": {"Clip": {"required": ["Title"], "properties": {
        "Title": {"status": "used"}, "Extra": {"type": "Guid", "status": "sent", "description": "Added."}}}}}), encoding="utf-8")
    out = tmp_path / "cat.json"
    assert mod.main([str(spec), "--out", str(out), "--overrides", str(ov), "--type-overrides", str(tov)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    by = {m["name"]: m for m in data["methods"]}
    assert by["GetApiLevel"]["service"] == "Configuration"
    assert by["GetApiLevel"]["params"] == [{"name": "apiType", "type": "ApiType"}]
    assert by["GetApiLevel"]["valid_ticket_only"] is True and by["GetApiLevel"]["auth_ticket"] == "omit"
    assert by["GetApiLevel"]["returns"] == "string"
    assert by["ClipFile"]["auth_ticket"] == "empty_object" and by["ClipFile"]["returns"] == "Guid"
    assert by["ClipFile"]["params"] == [
        {"name": "clipProperties", "type": "Clip"}, {"name": "when", "type": "DateTime"}, {"name": "ids", "type": "Guid[]"}]
    assert "depends on the input" in by["ClipFile"]["note"]
    assert by["NewThing"]["access"] == "write" and data["defaulted_to_write"] == ["TaskManager/NewThing"]
    assert by["Exists"]["access"] == "blocked" and by["Exists"]["not_in_spec"] is True
    assert by["TaskRunning"]["read_only_comment"] == "spec flag is wrong"
    svc = {s["service"]: s for s in data["services"]}
    assert svc["Configuration"]["url_path"] == "Config" and svc["Configuration"]["interface"] == "IConfigurationApi"
    assert svc["FileAccess"]["in_spec"] is False
    sch = data["schemas"]
    assert sch["Flags"] == {"description": "F.", "kind": "flags", "values": {"One": 1, "Two": 2, "Both": 3}}
    assert sch["ApiType"]["values"] == {"A": 0, "B": 1}
    assert sch["Clip"]["base"] == "Base" and sch["Clip"]["type_hint"] == "Clip:#SnapStream.Services"
    assert sch["Clip"]["required"] == ["Title"] and sch["Clip"]["properties"]["Extra"]["type"] == "Guid"
    assert sch["Clip"]["properties"]["Title"]["status"] == "used"
    assert data["tick_fields"] == ["StartPosition"]
    assert EM_DASH not in out.read_text(encoding="utf-8")
    assert Catalog(data).get("Config", "GetApiLevel").path == "Config"


def test_builder_openapi_refuses_unexplained_read_only_conflict(tmp_path):
    mod = _builder()
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(SYNTH_SPEC), encoding="utf-8")
    ov = tmp_path / "ov.json"
    _write_overrides(ov, [{"service": "TaskManager", "method": "TaskRunning", "access": "read", "reason": "query"}])
    with pytest.raises(SystemExit, match="TaskRunning"):
        mod.main([str(spec), "--out", str(tmp_path / "c.json"), "--overrides", str(ov)])
    _write_overrides(ov, [{"service": "FileAccess", "method": "Exists", "access": "read", "reason": "x", "not_in_spec": {"summary": "x"}}])
    with pytest.raises(SystemExit, match="must be blocked"):
        mod.main([str(spec), "--out", str(tmp_path / "c.json"), "--overrides", str(ov)])


# --------------------- source-item fields are accepted on every clip type (api-2)
SOURCE_ITEM_FIELDS = ("ItemId", "FolderPathId", "FolderId", "MachineId", "DriveId", "FullPath",
                      "SourceFilename", "Filename", "FileName", "LiveTvSessionInfo")


@pytest.mark.parametrize("typ", [
    "BaseClipProperties", "ClipProperties", "ClipSlackProperties", "ClipTweetProperties",
    "ClipYouTubeProperties", "ClipFacebookProperties",
])
def test_source_item_fields_on_every_clip_type(catalog, typ):
    fields = catalog.fields(typ)
    for f in SOURCE_ITEM_FIELDS:
        assert f in fields, (typ, f)
    value = {f: None for f in SOURCE_ITEM_FIELDS}
    assert catalog.validate_nested(typ, value, "props") == []
    # Social-only fields stay on ClipProperties.
    if typ != "ClipProperties":
        assert catalog.validate_nested(typ, {"Subtitles": True}, "props") != []
