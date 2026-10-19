from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        hide_input_in_errors=True,
    )
    bot_token: str = Field(repr=False)
    main_admin_id: int = Field(gt=0)
    api_id: int = Field(gt=0)
    api_hash: str = Field(min_length=32, max_length=32, repr=False)
    database_url: str = Field(repr=False)
    session_encryption_key: str = Field(repr=False)
    timezone: str = "Europe/Moscow"
    check_interval_seconds: int = Field(default=300, ge=60, le=86400)
    floodwait_notify_seconds: int = Field(default=21600, ge=60)
    report_retention_days: int = Field(default=30, ge=1, le=3650)
    report_reminder_days: int = Field(default=3, ge=0)
    backup_interval_days: int = Field(default=3, ge=1, le=365)
    backup_mirror_dir: Path | None = None
    backup_age_recipient: str | None = None
    backup_keep_count: int = Field(default=20, ge=2, le=1000)
    backup_timeout_seconds: int = Field(default=600, ge=30, le=7200)
    max_concurrent_accounts: int = Field(default=5, ge=1, le=50)
    max_queued_jobs: int = Field(default=50, ge=1, le=500)
    max_channels_per_job: int = Field(default=50, ge=1, le=200)
    rpc_timeout_seconds: int = Field(default=60, ge=10, le=300)
    shutdown_timeout_seconds: int = Field(default=30, ge=5, le=120)
    proxy_policy: Literal["required", "allow_direct"] = "required"
    proxy_check_max_age_hours: int = Field(default=24, ge=1, le=168)
    manager_show_phones: bool = True
    healthcheck_interval_hours: int = Field(default=6, ge=1, le=168)
    max_import_accounts: int = Field(default=100, ge=1, le=500)
    max_upload_bytes: int = Field(default=5_000_000, ge=1024, le=20_000_000)
    max_import_expanded_bytes: int = Field(default=20_000_000, ge=1024, le=100_000_000)
    login_ttl_seconds: int = Field(default=300, ge=60, le=900)
    data_dir: Path = Path("./data")
    reports_dir: Path = Path("./reports")
    backups_dir: Path = Path("./backups")
    logs_dir: Path = Path("./logs")
    log_level: str = "INFO"

    @field_validator("bot_token")
    @classmethod
    def token_shape(cls, value):
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{30,}", value):
            raise ValueError("Некорректный формат BOT_TOKEN")
        return value

    @field_validator("database_url")
    @classmethod
    def postgres_only(cls, value: str) -> str:
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL должен начинаться с postgresql+asyncpg://")
        return value

    @field_validator("session_encryption_key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        try:
            Fernet(value.strip().encode("ascii"))
        except (ValueError, UnicodeError) as exc:
            raise ValueError("Некорректный ключ Fernet") from exc
        return value.strip()

    @field_validator("timezone")
    @classmethod
    def timezone_exists(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @field_validator("api_hash")
    @classmethod
    def hash_is_hex(cls, value: str) -> str:
        if any(ch not in "0123456789abcdefABCDEF" for ch in value):
            raise ValueError("API_HASH должен содержать 32 шестнадцатеричных символа")
        return value

    @model_validator(mode="after")
    def coherent_retention(self):
        if bool(self.backup_mirror_dir) != bool(self.backup_age_recipient):
            raise ValueError("BACKUP_MIRROR_DIR и BACKUP_AGE_RECIPIENT задаются вместе")
        if self.report_reminder_days >= self.report_retention_days:
            raise ValueError("REPORT_REMINDER_DAYS должен быть меньше REPORT_RETENTION_DAYS")
        return self

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def ensure_directories(self) -> None:
        os.umask(0o077)
        for path in (self.data_dir, self.reports_dir, self.backups_dir, self.logs_dir):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.chmod(0o700)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(_env_file=os.environ.get("TGVIEWER_ENV_FILE", ".env"))
