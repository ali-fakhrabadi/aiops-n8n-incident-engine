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
MIN_SIGNING_KEY_LENGTH = 32


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


class RuntimeSettings(DatabaseSettings):
    """Settings shared by the API and the worker."""

    secret_fields: ClassVar[tuple[str, ...]] = ("db_password", "redis_password")

    environment: Literal["development", "test", "production"] = "production"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    redis_host: str = "localhost"
    redis_port: int = Field(default=6379, ge=1, le=65535)
    redis_db: int = Field(default=0, ge=0, le=15)
    redis_password: SecretStr | None = None
    redis_password_file: Path | None = None
    redis_timeout_seconds: float = Field(default=2.0, gt=0, le=60)

    # Incident events travel from the outbox table to this Redis stream.
    stream_name: str = "aiops:incidents"
    dead_letter_stream_name: str = "aiops:incidents:dead"
    consumer_group: str = "aiops-workers"
    # Keep roughly this many entries per stream (XADD MAXLEN ~).
    stream_max_length: int = Field(default=100_000, ge=1000)


class Settings(RuntimeSettings):
    """Settings of the API process."""

    secret_fields: ClassVar[tuple[str, ...]] = (
        "db_password",
        "redis_password",
        "metrics_token",
        "ingest_token",
        "api_token",
        "callback_token",
    )

    # Bearer token Prometheus presents to scrape /metrics.
    metrics_token: SecretStr
    metrics_token_file: Path | None = None
    # Bearer token Alertmanager presents to deliver alerts.
    ingest_token: SecretStr
    ingest_token_file: Path | None = None
    # Bearer token for reading incidents (operators, dashboards).
    api_token: SecretStr
    api_token_file: Path | None = None
    # Bearer token n8n presents to report deliveries; it can do nothing else.
    callback_token: SecretStr
    callback_token_file: Path | None = None

    readiness_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    max_request_bytes: int = Field(default=1_048_576, ge=1024)
    max_alerts_per_request: int = Field(default=500, ge=1, le=10_000)

    # The same alert opening this many incidents within the window is flapping.
    flap_threshold: int = Field(default=3, ge=2)
    flap_window_minutes: int = Field(default=60, ge=1)

    @field_validator("metrics_token", "ingest_token", "api_token", "callback_token")
    @classmethod
    def _long_enough(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < MIN_TOKEN_LENGTH:
            msg = f"must be at least {MIN_TOKEN_LENGTH} characters"
            raise ValueError(msg)
        return value


class WorkerSettings(RuntimeSettings):
    """Settings of the worker process (outbox relay and stream consumer)."""

    secret_fields: ClassVar[tuple[str, ...]] = (
        "db_password",
        "redis_password",
        "openai_api_key",
        "n8n_webhook_secret",
    )

    # Root-cause analysis. "mock" is deterministic and needs no model (CI, demos);
    # "ollama" keeps every alert and log line on this host; "openai" is any
    # OpenAI-compatible chat completions API.
    analyzer: Literal["mock", "ollama", "openai"] = "mock"
    llm_model: str = "llama3.1"
    ollama_url: str = "http://host.docker.internal:11434"
    openai_base_url: str = "https://api.openai.com"
    openai_api_key: SecretStr | None = None
    openai_api_key_file: Path | None = None
    # A CPU-only 8B model needs a minute or two per incident.
    llm_timeout_seconds: float = Field(default=300.0, gt=0, le=1800)
    llm_num_ctx: int = Field(default=8192, ge=2048, le=131_072)
    # How much of the collected log context goes into the prompt.
    llm_max_log_lines: int = Field(default=150, ge=0, le=2000)
    llm_max_log_chars: int = Field(default=12_000, ge=0, le=200_000)

    # Loki to collect log context from; empty disables log collection.
    loki_url: str | None = None
    loki_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    loki_lookback_minutes: int = Field(default=15, ge=1, le=360)
    loki_lookahead_minutes: int = Field(default=2, ge=0, le=60)
    loki_max_lines: int = Field(default=200, ge=1, le=5000)
    loki_max_line_length: int = Field(default=2000, ge=100, le=65_536)

    # Notifications: n8n's webhook for the incident routing workflow; empty disables
    # them. Requests carry an HS256 token signed with the webhook secret, which n8n
    # keeps as a JWT credential.
    n8n_webhook_url: str | None = None
    n8n_webhook_secret: SecretStr | None = None
    n8n_webhook_secret_file: Path | None = None
    # n8n answers once it has checked the signature, before it calls the channel.
    n8n_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    # Attempts per stream delivery, with exponential back-off between them; after
    # that the message is retried like any other (claim_idle_seconds).
    n8n_attempts: int = Field(default=3, ge=1, le=10)
    n8n_backoff_seconds: float = Field(default=2.0, ge=0, le=60)
    notification_token_ttl_seconds: int = Field(default=300, ge=30, le=3600)

    relay_poll_seconds: float = Field(default=0.5, gt=0, le=60)
    relay_batch_size: int = Field(default=100, ge=1, le=1000)
    read_block_ms: int = Field(default=2000, ge=100, le=60_000)
    # A message not acknowledged for this long is claimed again (retry with back-off).
    # Keep it above llm_timeout_seconds, or a second worker may claim an analysis that
    # is still running.
    claim_idle_seconds: float = Field(default=600.0, ge=0)
    max_deliveries: int = Field(default=5, ge=1, le=100)
    consumer_name: str | None = None
    # 0 picks a free port (tests run several workers in one host).
    metrics_port: int = Field(default=9101, ge=0, le=65535)
    heartbeat_file: Path = Path("/tmp/aiops-worker-heartbeat")  # noqa: S108 - tmpfs in the container

    @field_validator("loki_url", "n8n_webhook_url")
    @classmethod
    def _optional_http_url(cls, value: str | None) -> str | None:
        return _http_url(value) if value else None

    @field_validator("n8n_webhook_secret")
    @classmethod
    def _strong_secret(cls, value: SecretStr | None) -> SecretStr | None:
        # HS256 keys shorter than the hash output weaken the signature (RFC 7518 3.2).
        if value is not None and len(value.get_secret_value()) < MIN_SIGNING_KEY_LENGTH:
            msg = f"must be at least {MIN_SIGNING_KEY_LENGTH} characters"
            raise ValueError(msg)
        return value

    @field_validator("ollama_url", "openai_base_url")
    @classmethod
    def _required_http_url(cls, value: str) -> str:
        return _http_url(value)

    @model_validator(mode="after")
    def _analyzer_is_configured(self) -> WorkerSettings:
        if self.analyzer == "openai" and self.openai_api_key is None:
            msg = "analyzer 'openai' needs openai_api_key (or openai_api_key_file)"
            raise ValueError(msg)
        if self.n8n_webhook_url and self.n8n_webhook_secret is None:
            msg = "n8n_webhook_url needs n8n_webhook_secret (or n8n_webhook_secret_file)"
            raise ValueError(msg)
        return self


def _http_url(value: str) -> str:
    if not value.startswith(("http://", "https://")):
        msg = "must be an http:// or https:// URL"
        raise ValueError(msg)
    return value.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    """Settings of the API process, read once from the environment."""
    return Settings()
