"""Runtime settings from AIOPS_* environment variables.

Every secret can also come from a file, AIOPS_<NAME>_FILE, which is how Docker secrets
are mounted; secrets then never appear in `docker inspect` or the process environment.
Setting both the value and the file of one secret is rejected as ambiguous.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_TOKEN_LENGTH = 16


def _read_secret_file(path: Path, setting: str) -> str:
    try:
        value = path.read_text(encoding="utf-8").rstrip("\r\n")
    except OSError as exc:
        msg = f"{setting}: cannot read {path} ({exc.strerror})"
        raise ValueError(msg) from exc
    if not value:
        msg = f"{setting}: {path} is empty"
        raise ValueError(msg)
    return value


class SecretFileSettings(BaseSettings):
    """Resolves `<name>_file` settings into the secret values they point to."""

    model_config = SettingsConfigDict(env_prefix="AIOPS_", extra="ignore", frozen=True)

    secret_fields: ClassVar[tuple[str, ...]] = ()

    @model_validator(mode="before")
    @classmethod
    def _resolve_secret_files(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        resolved = dict(data)
        for name in cls.secret_fields:
            file_setting = f"{name}_file"
            path = resolved.pop(file_setting, None)
            if path in (None, ""):
                continue
            if resolved.get(name) not in (None, ""):
                msg = f"set either {name} or {file_setting}, not both"
                raise ValueError(msg)
            resolved[name] = _read_secret_file(Path(path), file_setting)
        return resolved


class DatabaseSettings(SecretFileSettings):
    """PostgreSQL connection settings; also all that Alembic needs."""

    secret_fields: ClassVar[tuple[str, ...]] = ("db_password",)

    db_host: str = "localhost"
    db_port: int = Field(default=5432, ge=1, le=65535)
    db_name: str = "aiops"
    db_user: str = "aiops_app"
    db_password: SecretStr
    db_password_file: Path | None = None
    db_pool_size: int = Field(default=5, ge=1, le=100)
    db_max_overflow: int = Field(default=5, ge=0, le=100)
    db_statement_timeout_ms: int = Field(default=5000, ge=100)
    db_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    db_application_name: str = "aiops-engine"


class Settings(DatabaseSettings):
    """Settings of the API process."""

    secret_fields: ClassVar[tuple[str, ...]] = ("db_password", "redis_password", "metrics_token")

    environment: Literal["development", "test", "production"] = "production"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    redis_host: str = "localhost"
    redis_port: int = Field(default=6379, ge=1, le=65535)
    redis_db: int = Field(default=0, ge=0, le=15)
    redis_password: SecretStr | None = None
    redis_password_file: Path | None = None
    redis_timeout_seconds: float = Field(default=2.0, gt=0, le=60)

    # Bearer token Prometheus presents to scrape /metrics.
    metrics_token: SecretStr
    metrics_token_file: Path | None = None

    readiness_timeout_seconds: float = Field(default=2.0, gt=0, le=30)

    @field_validator("metrics_token")
    @classmethod
    def _long_enough(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < MIN_TOKEN_LENGTH:
            msg = f"must be at least {MIN_TOKEN_LENGTH} characters"
            raise ValueError(msg)
        return value


@lru_cache
def get_settings() -> Settings:
    """Settings of this process, read once from the environment."""
    return Settings()
