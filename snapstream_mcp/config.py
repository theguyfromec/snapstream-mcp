"""Load and validate secrets.yaml plus the tunable runtime settings.

Error messages name keys and files, never values. The Config object hides every secret from
repr() so an accidental log line or traceback cannot print one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import yaml

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent
DEFAULT_SECRETS_PATH = PROJECT_DIR / "secrets.yaml"
SECRETS_ENV = "SNAPSTREAM_MCP_SECRETS"

REQUIRED_KEYS = ("base_url", "token_endpoint", "client_id", "client_secret", "api_key")
OPTIONAL_KEYS = ("instance", "scopes")
SECRET_KEYS = ("client_id", "client_secret", "api_key")
PLACEHOLDER = "replace_me"


class ConfigError(Exception):
    """Configuration problem. The message never contains a configured value."""


def _env_float(name: str, default: float, minimum: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        raise ConfigError(f"Environment variable {name} must be a number.") from None


def _env_int(name: str, default: int, minimum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        raise ConfigError(f"Environment variable {name} must be an integer.") from None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "on", "true", "yes"):
        return True
    if raw in ("0", "off", "false", "no"):
        return False
    raise ConfigError(f"Environment variable {name} must be on or off.")


# The API gateway gives up after 29 s, so the client timeout stays under it.
DEFAULT_TIMEOUT = 28.0
CALL_COUNTER_NAME = "snapstream_calls.daily.json"


@dataclass(frozen=True)
class Settings:
    """Runtime limits. Each can be overridden by an environment variable."""

    timeout_seconds: float = DEFAULT_TIMEOUT  # SNAPSTREAM_MCP_TIMEOUT
    max_concurrency: int = 4               # SNAPSTREAM_MCP_MAX_CONCURRENCY
    write_min_interval: float = 5.0        # SNAPSTREAM_MCP_WRITE_MIN_INTERVAL (seconds between write calls)
    max_response_chars: int = 40000        # SNAPSTREAM_MCP_MAX_RESPONSE_CHARS
    stage_ttl_seconds: int = 600           # fixed: staged writes expire after 10 minutes
    audit_file: Path = PROJECT_DIR / "snapstream_writes.audit.jsonl"  # SNAPSTREAM_MCP_AUDIT_FILE
    empty_auth_ticket: bool = True         # SNAPSTREAM_MCP_EMPTY_AUTH_TICKET (on|off)
    rate_per_sec: float = 8.0              # SNAPSTREAM_MCP_RATE_PER_SEC (token bucket refill, all calls)
    burst: int = 16                        # SNAPSTREAM_MCP_BURST (token bucket size)
    daily_budget: int = 20000              # SNAPSTREAM_MCP_DAILY_BUDGET (soft cap on API calls per UTC day)
    read_attempts: int = 3                 # fixed: a read is tried at most 3 times on 429/503

    @property
    def call_counter_file(self) -> Path:
        """The UTC-day call counter, kept beside the audit file."""
        return self.audit_file.with_name(CALL_COUNTER_NAME)

    @classmethod
    def from_env(cls) -> "Settings":
        audit = os.environ.get("SNAPSTREAM_MCP_AUDIT_FILE", "").strip()
        return cls(
            timeout_seconds=_env_float("SNAPSTREAM_MCP_TIMEOUT", DEFAULT_TIMEOUT, 1.0),
            max_concurrency=_env_int("SNAPSTREAM_MCP_MAX_CONCURRENCY", 4, 1),
            write_min_interval=_env_float("SNAPSTREAM_MCP_WRITE_MIN_INTERVAL", 5.0, 0.0),
            max_response_chars=_env_int("SNAPSTREAM_MCP_MAX_RESPONSE_CHARS", 40000, 2000),
            audit_file=Path(audit) if audit else PROJECT_DIR / "snapstream_writes.audit.jsonl",
            empty_auth_ticket=_env_bool("SNAPSTREAM_MCP_EMPTY_AUTH_TICKET", True),
            rate_per_sec=_env_float("SNAPSTREAM_MCP_RATE_PER_SEC", 8.0, 0.1),
            burst=_env_int("SNAPSTREAM_MCP_BURST", 16, 1),
            daily_budget=_env_int("SNAPSTREAM_MCP_DAILY_BUDGET", 20000, 1),
        )


@dataclass(frozen=True)
class Config:
    """Validated credentials for one SnapStream instance. Secrets are excluded from repr()."""

    base_url: str
    token_endpoint: str = field(repr=False)
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    api_key: str = field(repr=False)
    instance: str = ""
    scopes: str = ""
    source_path: str = ""

    @property
    def base_host(self) -> str:
        return urlparse(self.base_url).hostname or ""

    @property
    def token_host(self) -> str:
        return urlparse(self.token_endpoint).hostname or ""

    def secret_values(self) -> List[str]:
        """Every credential value that must never appear in output."""
        vals = [self.client_id, self.client_secret, self.api_key]
        return [v for v in vals if v]


def secrets_path() -> Path:
    override = os.environ.get(SECRETS_ENV, "").strip()
    return Path(override).expanduser() if override else DEFAULT_SECRETS_PATH


def _is_placeholder(value: str) -> bool:
    return PLACEHOLDER in value.lower()


def _require_https(key: str, value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ConfigError(f"secrets.yaml key '{key}' must be an https:// URL with a host name.")
    if parsed.username or parsed.password:
        raise ConfigError(f"secrets.yaml key '{key}' must not embed credentials in the URL.")


def parse_config(data: Any, source: str = "secrets.yaml") -> Config:
    """Validate a parsed secrets mapping. Raises ConfigError naming the key, never the value."""
    if not isinstance(data, dict):
        raise ConfigError(f"{source} must be a YAML mapping of keys to values.")
    clean: Dict[str, str] = {}
    for key in REQUIRED_KEYS + OPTIONAL_KEYS:
        raw = data.get(key)
        if raw is None:
            value = ""
        elif isinstance(raw, (str, int, float)) and not isinstance(raw, bool):
            value = str(raw).strip()
        else:
            raise ConfigError(f"{source} key '{key}' must be a plain string.")
        if key in REQUIRED_KEYS and not value:
            raise ConfigError(f"{source} is missing required key '{key}' (or it is empty).")
        if value and _is_placeholder(value):
            raise ConfigError(f"{source} key '{key}' still holds the REPLACE_ME placeholder. Fill it in from Cloud Admin.")
        clean[key] = value
    _require_https("base_url", clean["base_url"])
    _require_https("token_endpoint", clean["token_endpoint"])
    return Config(
        base_url=clean["base_url"].rstrip("/"),
        token_endpoint=clean["token_endpoint"],
        client_id=clean["client_id"],
        client_secret=clean["client_secret"],
        api_key=clean["api_key"],
        instance=clean["instance"],
        scopes=" ".join(clean["scopes"].split()),
        source_path=source,
    )


def load_config(path: Optional[Path] = None) -> Config:
    """Read and validate secrets.yaml (or the file named by SNAPSTREAM_MCP_SECRETS)."""
    p = Path(path) if path else secrets_path()
    if not p.is_file():
        raise ConfigError(
            f"Secrets file not found at {p}. Copy secrets.yaml.example to secrets.yaml and fill it in, "
            f"or point {SECRETS_ENV} at it."
        )
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        # The YAML error text can quote file content, so it is not passed through.
        raise ConfigError(f"Secrets file {p.name} is not valid YAML.") from None
    except OSError as exc:
        raise ConfigError(f"Secrets file {p.name} could not be read ({type(exc).__name__}).") from None
    return parse_config(data, source=p.name)
