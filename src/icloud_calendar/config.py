"""Runtime configuration, read from environment variables (or a .env file)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- iCloud / CalDAV -------------------------------------------------
    icloud_username: str = Field(default="", description="Apple ID email address used to sign in to iCloud.")
    icloud_app_password: SecretStr = Field(
        default=SecretStr(""),
        description="App-specific password from account.apple.com (NOT your Apple ID password).",
    )
    caldav_url: str = Field(
        default="https://caldav.icloud.com/",
        description="CalDAV server root. Only change this for testing against another server.",
    )
    default_timezone: str = Field(
        default="UTC", description="IANA timezone used for display and for naive input times."
    )
    default_calendar: str | None = Field(
        default=None, description="Calendar name or id used when a tool call does not name one."
    )
    http_timeout: float = 30.0

    # --- HTTP server / auth ----------------------------------------------
    public_url: str = Field(
        default="http://localhost:8000",
        description="Externally reachable base URL of this server (used as OAuth issuer).",
    )
    host: str = "0.0.0.0"
    port: int = 8000
    api_key: SecretStr | None = Field(
        default=None,
        description="Static bearer token accepted by /mcp and /api/v1 (for scripts, Claude Code).",
    )
    owner_password: SecretStr | None = Field(
        default=None,
        description="Password you type on the OAuth login page when connecting claude.ai.",
    )
    data_dir: Path = Field(default=Path("./data"), description="Where OAuth state is persisted.")
    access_token_ttl_seconds: int = 60 * 60 * 24  # 1 day
    refresh_token_ttl_seconds: int = 60 * 60 * 24 * 90  # 90 days

    @field_validator("default_timezone")
    @classmethod
    def _valid_tz(cls, v: str) -> str:
        ZoneInfo(v)  # raises if unknown
        return v

    @field_validator("public_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.default_timezone)


@lru_cache
def get_settings() -> Settings:
    return Settings()
