import json

import pytest

from snapstream_mcp.formatting import (
    EMPTY_GUID,
    cap_output,
    compact,
    format_response,
    iso_duration_seconds,
    normalize,
    parse_time_value,
    redact_fields,
    seconds_to_clock,
    shape,
    to_wcf_date,
    unwrap_result,
    wcf_date_to_iso,
)


def test_wcf_dates():
    assert wcf_date_to_iso("/Date(0)/") == "1970-01-01T00:00:00Z"
    assert wcf_date_to_iso("/Date(1795552679779)/") == "2026-11-24T20:37:59.779Z"
    # The offset records the server zone; the ms value is already UTC.
    assert wcf_date_to_iso("/Date(1795552679779-0600)/") == "2026-11-24T20:37:59.779Z"
    assert wcf_date_to_iso("/Date(1795552679779+0530)/") == "2026-11-24T20:37:59.779Z"
    assert wcf_date_to_iso("/Date(-62135596800000)/") is None  # min date means unset
    assert wcf_date_to_iso("/Date(-62135596800000+0000)/") is None


def test_durations():
    assert iso_duration_seconds("PT37M58.4304474S") == pytest.approx(2278.43)
    assert iso_duration_seconds("PT1H") == 3600
    assert iso_duration_seconds("P1DT2H") == 93600
    assert iso_duration_seconds("PT0S") == 0
    assert iso_duration_seconds("-PT30S") == -30
    assert iso_duration_seconds("PT") is None
    assert iso_duration_seconds("Pizza") is None


def test_normalize_structure():
    raw = {
        "Id": "3FA85F64-5717-4562-B3FC-2C963F66AFA6",
        "ParentId": EMPTY_GUID,
        "Start": "/Date(1795552679779)/",
        "Deleted": "/Date(-62135596800000)/",
        "Duration": "PT37M58.4304474S",
        "Title": "Evening News",
        "Clips": [{"End": "/Date(0+0000)/", "Length": "PT10S"}],
        "Flag": False,
        "Count": 0,
    }
    out = normalize(raw)
    assert out["ParentId"] is None
    assert out["Start"] == "2026-11-24T20:37:59.779Z"
    assert out["Deleted"] is None
    assert out["Duration"] == "PT37M58.4304474S"
    assert out["Duration_seconds"] == pytest.approx(2278.43)
    assert out["Clips"][0] == {"End": "1970-01-01T00:00:00Z", "Length": "PT10S", "Length_seconds": 10}
    assert out["Title"] == "Evening News"
    c = compact(out)
    assert "ParentId" not in c and "Deleted" not in c
    assert c["Flag"] is False and c["Count"] == 0


def test_unwrap():
    assert unwrap_result({"GetMediaItemsResult": [1]}, "GetMediaItems") == [1]
    assert unwrap_result({"SomethingResult": 5}) == 5
    assert unwrap_result({"a": 1, "b": 2}) == {"a": 1, "b": 2}
    assert unwrap_result(None) is None


def test_format_response_end_to_end():
    out = format_response({"GetMediaItemsResult": [{"Id": EMPTY_GUID, "Start": "/Date(0)/"}]}, "GetMediaItems", max_chars=5000)
    assert out == {"result": [{"Start": "1970-01-01T00:00:00Z"}], "truncated": False}


def test_cap_output_list():
    data = [{"i": i, "pad": "x" * 50} for i in range(100)]
    out = cap_output(data, 1000, index=20, paged=True)
    assert out["truncated"] is True
    n = out["items_returned"]
    assert 0 < n < 100
    assert len(json.dumps(out["result"])) <= 1000
    assert f"index={20 + n}" in out["hint"]


def test_cap_output_dict_trims_largest_list():
    data = {"Total": 500, "Results": [{"pad": "y" * 80} for _ in range(100)]}
    out = cap_output(data, 1500)
    assert out["truncated"] is True and out["trimmed_field"] == "Results"
    assert out["result"]["Total"] == 500
    assert len(out["result"]["Results"]) == out["items_returned"] < 100


def test_cap_output_scalar_text():
    out = cap_output("z" * 5000, 2000)
    assert out["truncated"] is True and len(out["result_text"]) == 2000


def test_to_wcf_date():
    assert to_wcf_date("1970-01-01T00:00:10Z") == "/Date(10000)/"
    assert to_wcf_date("1970-01-01T01:00:10+01:00") == "/Date(10000)/"
    assert to_wcf_date("1970-01-01T00:00:10") == "/Date(10000)/"  # naive is UTC
    assert to_wcf_date("/Date(5)/") == "/Date(5)/"
    assert to_wcf_date(1234) == "/Date(1234)/"
    assert to_wcf_date("now").startswith("/Date(")
    with pytest.raises(ValueError):
        to_wcf_date("yesterday-ish")


def test_redact_fields_default_and_strict():
    data = {"Name": "Dest", "Password": "p@ss", "Nested": {"ApiKey": "k", "SecretSet": True}, "UserName": "bob", "UserId": EMPTY_GUID}
    d = redact_fields(data)
    assert d["Password"] == "[REDACTED]" and d["Nested"]["ApiKey"] == "[REDACTED]"
    assert d["Nested"]["SecretSet"] is True  # booleans reveal nothing
    assert d["UserName"] == "bob"
    s = redact_fields(data, strict=True)
    assert s["UserName"] == "[REDACTED]"
    assert s["UserId"] == EMPTY_GUID  # identifiers are kept


def test_export_destination_responses_use_strict_redaction():
    raw = {"GetExportDestinationsResult": [{"Name": "FTP", "Host": "ftp.example.test", "Login": "svc", "Password": "x"}]}
    out = format_response(raw, "GetExportDestinations", max_chars=5000)
    item = out["result"][0]
    assert item["Login"] == "[REDACTED]" and item["Password"] == "[REDACTED]" and item["Name"] == "FTP"


def test_shape_has_no_values():
    s = json.dumps(shape({"Title": "secret show", "Items": [{"a": 1}]}))
    assert "secret show" not in s and "Title" in s


def test_shifted_min_date_is_unset():
    # DateTime.MinValue of kind Local, serialized on a host west of UTC (UTC-6).
    assert wcf_date_to_iso("/Date(-62135575200000-0600)/") is None
    assert wcf_date_to_iso("/Date(-62135546400000-1400)/") is None  # the widest offset
    assert normalize({"Start": "/Date(-62135575200000-0600)/"}) == {"Start": None}
    assert compact(normalize({"Start": "/Date(-62135575200000-0600)/", "Title": "x"})) == {"Title": "x"}
    assert parse_time_value("/Date(-62135575200000-0600)/") == (None, "")
    # A real year 1 instant more than 14 hours past the min date is still a date.
    assert wcf_date_to_iso("/Date(-62135510400000)/") == "0001-01-02T00:00:00Z"


def test_parse_time_value_uses_field_name_and_magnitude():
    assert parse_time_value(62000, "StartMs") == (62.0, "offset_s")
    assert parse_time_value(62000, "StartMilliseconds") == (62.0, "offset_s")
    assert parse_time_value(62, "StartSeconds") == (62.0, "offset_s")
    assert seconds_to_clock(parse_time_value(62000, "startms")[0]) == "00:01:02"
    # Epoch milliseconds are absolute instants, whatever the field is called.
    assert parse_time_value(1795552679779, "Start") == (1795552679.779, "epoch_s")
    assert parse_time_value(1795552679779, "StartMs") == (1795552679.779, "epoch_s")
    assert parse_time_value(1795552679, "StartSeconds") == (1795552679.0, "epoch_s")
    # Unknown unit keeps the old fallback.
    assert parse_time_value(62, "Start") == (62.0, "offset_s")
    assert parse_time_value(62_000_000, "Start") == (62000.0, "offset_s")
    assert parse_time_value(True, "Start") == (None, "")


def test_transcript_window_with_ms_and_epoch_ms_fields():
    from snapstream_mcp.server import transcript_window

    ms = transcript_window([{"StartMs": 62000, "Text": "a"}, {"StartMs": 125000, "Text": "b"}])
    assert ms["transcript"] == "[00:01:02] a\n[00:02:05] b"
    ep = transcript_window([{"Start": 1795552679779, "Text": "a"}, {"Start": 1795552741779, "Text": "b"}])
    assert ep["time_basis"] == "seconds from the first caption"
    assert ep["transcript"] == "[00:00:00] a\n[00:01:02] b"


def test_strict_write_method_pattern():
    from snapstream_mcp.formatting import STRICT_REDACT_WRITE_METHODS as P

    for m in ("SaveExportDestination", "AddExportDestination", "AddTaskAction", "SaveTaskAction",
              "StreamToYouTubeLive", "StreamToFacebookLive", "FederatedUpload", "FederatedPlaylistDownload"):
        assert P.search(m), m
    for m in ("SaveBookmark", "RecordAiring", "ClipFile"):
        assert not P.search(m), m


# ------------------------------------------------------------------- ticks
from snapstream_mcp.formatting import (  # noqa: E402
    TICKS_PER_SECOND,
    parse_clip_time,
    seconds_to_ticks,
    ticks_to_seconds,
)


def test_tick_helpers():
    assert TICKS_PER_SECOND == 10_000_000
    assert seconds_to_ticks(62) == 620_000_000
    assert seconds_to_ticks(75.5) == 755_000_000
    assert seconds_to_ticks(0.0000001) == 1
    assert ticks_to_seconds(755_000_000) == 75.5
    assert ticks_to_seconds(18_123_456_789) == 1812.346
    assert ticks_to_seconds(seconds_to_ticks(3599.999)) == 3599.999


@pytest.mark.parametrize("value,seconds", [
    (62, 62.0), (62.5, 62.5), ("62.5", 62.5), ("00:01:02", 62.0), ("1:02", 62.0), ("01:01:02.250", 3662.25),
    ("ms:62000", 62.0), ("MS: 1500", 1.5), (0, 0.0),
])
def test_parse_clip_time(value, seconds):
    assert parse_clip_time(value) == pytest.approx(seconds)


@pytest.mark.parametrize("bad", ["abc", "ms:x", "00:61:00", -1, True, None, [], "1:2:3:4"])
def test_parse_clip_time_rejects(bad):
    with pytest.raises(ValueError):
        parse_clip_time(bad)


def test_normalize_adds_seconds_for_tick_fields():
    data = {"StartPosition": 620_000_000, "EndPosition": 755_000_000, "Tags": [{"StartTicks": 25_000_000, "Name": "x"}],
            "Start": 600_000_000, "TimeoutTicks": True}
    out = normalize(data)
    assert out["StartPosition_seconds"] == 62.0 and out["EndPosition_seconds"] == 75.5
    assert out["Tags"][0]["StartTicks_seconds"] == 2.5
    assert "Start_seconds" not in out  # Start is a tick field only when the caller says so
    assert "TimeoutTicks_seconds" not in out  # booleans are not ticks
    chapters = normalize([{"Start": 0, "End": 6_000_000_000}], tick_keys={"Start", "End"})
    assert chapters == [{"Start": 0, "Start_seconds": 0.0, "End": 6_000_000_000, "End_seconds": 600.0}]
    # An existing sibling is never overwritten.
    assert normalize({"StartPosition": 10_000_000, "StartPosition_seconds": 99})["StartPosition_seconds"] == 99
    out = format_response({"GetSmartChaptersResult": [{"Start": 10_000_000}]}, "GetSmartChapters",
                          max_chars=10_000, tick_keys={"Start"})
    assert out["result"] == [{"Start": 10_000_000, "Start_seconds": 1.0}]


def test_seconds_to_clock_fraction():
    assert seconds_to_clock(75.5, fraction=True) == "00:01:15.500"
    assert seconds_to_clock(62, fraction=True) == "00:01:02"
    assert seconds_to_clock(3662.25) == "01:01:02"
    assert seconds_to_clock(0.0004, fraction=True) == "00:00:00"
