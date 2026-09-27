# SPDX-License-Identifier: AGPL-3.0-or-later
"""Configuration: one YAML file for the service and its profiles, secrets from env or files.

Nothing secret lives in the YAML. AnkiWeb credentials are read from the ``credentials`` env-file
of each profile (``ANKIWEB_USERNAME`` / ``ANKIWEB_PASSWORD``) or from process env
``ANKIDO_PROFILE_<NAME>_USERNAME`` / ``_PASSWORD``. Files must be mode 0600 (or 0400) unless
``ANKIDO_ALLOW_INSECURE_SECRETS=1`` (for local development only).
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

AutosyncMode = Literal["after_write", "nightly", "off"]


class RateLimit(BaseModel):
    """Token-bucket parameters: ``per_minute`` sustained, ``burst`` instantaneous."""

    model_config = ConfigDict(extra="forbid")
    per_minute: int = Field(ge=1)
    burst: int = Field(ge=1)


class RateLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    read: RateLimit = RateLimit(per_minute=600, burst=120)
    write: RateLimit = RateLimit(per_minute=120, burst=40)
    sync: RateLimit = RateLimit(per_minute=6, burst=3)
    # OAuth consent, registration and token endpoints, keyed by client IP.
    oauth: RateLimit = RateLimit(per_minute=20, burst=10)


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bind: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    trusted_proxies: list[str] = Field(default_factory=list)
    cors_origins: list[str] = Field(default_factory=list)
    max_body_bytes: int = Field(default=16 * 1024 * 1024, ge=1024)
    operation_timeout_seconds: float = Field(default=30.0, gt=0)
    idle_close_seconds: float = Field(default=600.0, ge=0)
    rate_limits: RateLimits = RateLimits()
    log_level: str = "INFO"
    # External origin clients reach the service at, e.g. https://anki.example.com. Required for
    # MCP OAuth (claude.ai connectors); static tokens work without it.
    public_url: str | None = None

    @field_validator("public_url")
    @classmethod
    def _check_public_url(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        parsed = urlsplit(v.strip())
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("public_url must be an absolute URL like https://anki.example.com")
        if parsed.path.strip("/") or parsed.query or parsed.fragment:
            raise ValueError(
                "public_url must be an origin without a path (https://anki.example.com);"
                " serve Ankido at the root of its own hostname"
            )
        return f"{parsed.scheme}://{parsed.netloc}"


class MediaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url_allowlist: list[str] = Field(default_factory=list)
    max_bytes: int = Field(default=8 * 1024 * 1024, ge=1024)
    fetch_timeout_seconds: float = Field(default=15.0, gt=0)
    path_allowlist: list[Path] = Field(default_factory=list)


class BackupConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keep: int = Field(default=10, ge=1)


class ProfileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    collection: Path
    credentials: Path | None = None
    autosync: AutosyncMode = "after_write"
    after_write_debounce_seconds: float = Field(default=30.0, ge=0)
    nightly_at: str = "03:30"
    allow_schema_upgrade: bool = False
    sync_endpoint: str | None = None
    media_sync: bool = True
    backups: BackupConfig = BackupConfig()

    @field_validator("autosync", mode="before")
    @classmethod
    def _yaml_off(cls, v: object) -> object:
        # YAML parses a bare `off` as False; people will write it, so accept it.
        return "off" if v is False else v

    @field_validator("nightly_at")
    @classmethod
    def _check_time(cls, v: str) -> str:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError("nightly_at must be HH:MM (24h)")
        return v


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_dir: Path = Path("/data")
    server: ServerConfig = ServerConfig()
    media: MediaConfig = MediaConfig()
    profiles: dict[str, ProfileConfig]

    @model_validator(mode="after")
    def _check_profiles(self) -> Config:
        if not self.profiles:
            raise ValueError("at least one profile is required")
        for name in self.profiles:
            if not PROFILE_NAME_RE.fullmatch(name):
                raise ValueError(
                    f"profile name {name!r} is invalid: use lowercase letters, digits, '-' or '_'"
                )
        return self

    @property
    def state_db_path(self) -> Path:
        return self.data_dir / "ankido.db"

    def profile_dir(self, name: str) -> Path:
        return self.data_dir / name


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    username: str
    password: str

    def __repr__(self) -> str:  # never leak the password through repr()
        return f"Credentials(username={self.username!r}, password='***')"


DEFAULT_CONFIG_LOCATIONS = ("/config/ankido.yaml", "ankido.yaml")


def find_config_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get("ANKIDO_CONFIG")
    if env:
        return Path(env)
    for candidate in DEFAULT_CONFIG_LOCATIONS:
        if Path(candidate).is_file():
            return Path(candidate)
    raise FileNotFoundError(
        "no config found: pass --config, set ANKIDO_CONFIG, or create /config/ankido.yaml"
    )


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    p = find_config_path(path)
    with open(p, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: top level must be a mapping")
    return Config.model_validate(raw)


def _check_secret_file_mode(path: Path) -> None:
    if os.environ.get("ANKIDO_ALLOW_INSECURE_SECRETS") == "1":
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise PermissionError(
            f"{path}: secret file is readable by group/others (mode {mode:o}); chmod 600 it"
        )


def _parse_env_file(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def load_credentials(name: str, profile: ProfileConfig) -> Credentials | None:
    """Resolve AnkiWeb credentials for a profile, or ``None`` if none are configured."""
    env_key = name.upper().replace("-", "_")
    user = os.environ.get(f"ANKIDO_PROFILE_{env_key}_USERNAME")
    pw = os.environ.get(f"ANKIDO_PROFILE_{env_key}_PASSWORD")
    if user and pw:
        return Credentials(username=user, password=pw)
    if profile.credentials is None:
        return None
    path = profile.credentials
    if not path.is_file():
        raise FileNotFoundError(f"credentials file for profile {name!r} not found: {path}")
    _check_secret_file_mode(path)
    values = _parse_env_file(path.read_text(encoding="utf-8"))
    user = values.get("ANKIWEB_USERNAME") or values.get("USERNAME")
    pw = values.get("ANKIWEB_PASSWORD") or values.get("PASSWORD")
    if not user or not pw:
        raise ValueError(f"{path}: expected ANKIWEB_USERNAME and ANKIWEB_PASSWORD")
    return Credentials(username=user, password=pw)
