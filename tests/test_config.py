import pytest

from snapstream_mcp.config import ConfigError, Settings, load_config, parse_config

from .conftest import BASE_URL, FAKE_API_KEY, FAKE_CLIENT_ID, FAKE_CLIENT_SECRET, TOKEN_URL


def good():
    return {
        "instance": "demo.example.test",
        "base_url": BASE_URL + "/",
        "token_endpoint": TOKEN_URL,
        "client_id": FAKE_CLIENT_ID,
        "client_secret": FAKE_CLIENT_SECRET,
        "api_key": FAKE_API_KEY,
        "scopes": "  scope/a   scope/b ",
    }


def test_valid_config_parses_and_normalizes():
    c = parse_config(good())
    assert c.base_url == BASE_URL  # trailing slash removed
    assert c.base_host == "api.example.test"
    assert c.scopes == "scope/a scope/b"


def test_repr_hides_secrets():
    text = repr(parse_config(good()))
    for s in (FAKE_CLIENT_ID, FAKE_CLIENT_SECRET, FAKE_API_KEY, TOKEN_URL):
        assert s not in text


@pytest.mark.parametrize("key", ["base_url", "token_endpoint", "client_id", "client_secret", "api_key", "scopes"])
def test_placeholder_rejected(key):
    data = good()
    data[key] = "https://REPLACE_ME" if key in ("base_url", "token_endpoint") else "REPLACE_ME"
    with pytest.raises(ConfigError) as ei:
        parse_config(data)
    assert key in str(ei.value)
    assert "REPLACE_ME" in str(ei.value)


@pytest.mark.parametrize("key", ["base_url", "token_endpoint", "client_id", "client_secret", "api_key"])
def test_missing_required_key(key):
    data = good()
    del data[key]
    with pytest.raises(ConfigError, match=key):
        parse_config(data)


@pytest.mark.parametrize("key", ["base_url", "token_endpoint"])
def test_https_required(key):
    data = good()
    data[key] = data[key].replace("https://", "http://")
    with pytest.raises(ConfigError, match="https"):
        parse_config(data)


def test_error_text_never_contains_values():
    data = good()
    data["base_url"] = "http://user:" + FAKE_CLIENT_SECRET + "@host.example.test"
    with pytest.raises(ConfigError) as ei:
        parse_config(data)
    msg = str(ei.value)
    assert FAKE_CLIENT_SECRET not in msg and "host.example.test" not in msg


def test_load_from_env_path(tmp_path, monkeypatch):
    p = tmp_path / "s.yaml"
    p.write_text(
        "\n".join(f"{k}: \"{v}\"" for k, v in good().items()), encoding="utf-8"
    )
    monkeypatch.setenv("SNAPSTREAM_MCP_SECRETS", str(p))
    c = load_config()
    assert c.api_key == FAKE_API_KEY


def test_invalid_yaml_message_hides_content(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("client_secret: \"" + FAKE_CLIENT_SECRET + "\n  : : [", encoding="utf-8")
    with pytest.raises(ConfigError) as ei:
        load_config(p)
    assert FAKE_CLIENT_SECRET not in str(ei.value)


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("SNAPSTREAM_MCP_WRITE_MIN_INTERVAL", "12")
    monkeypatch.setenv("SNAPSTREAM_MCP_MAX_CONCURRENCY", "0")
    s = Settings.from_env()
    assert s.write_min_interval == 12.0
    assert s.max_concurrency == 1  # clamped to the minimum
    assert s.stage_ttl_seconds == 600
