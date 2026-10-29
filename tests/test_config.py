from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from aiops.config import Settings
from tests.conftest import METRICS_TOKEN, offline_settings


def test_secret_is_read_from_file_without_trailing_newline(tmp_path: Path) -> None:
    secret = tmp_path / "db_password"
    secret.write_text("s3cret-value\n", encoding="utf-8")

    settings = offline_settings(db_password=None, db_password_file=secret)

    assert settings.db_password.get_secret_value() == "s3cret-value"


def test_value_and_file_for_one_secret_are_rejected(tmp_path: Path) -> None:
    secret = tmp_path / "db_password"
    secret.write_text("from-file", encoding="utf-8")

    with pytest.raises(ValidationError, match="either db_password or db_password_file"):
        offline_settings(db_password="inline", db_password_file=secret)


def test_missing_secret_file_names_the_setting(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="metrics_token_file: cannot read"):
        offline_settings(metrics_token=None, metrics_token_file=tmp_path / "absent")


def test_empty_secret_file_is_rejected(tmp_path: Path) -> None:
    secret = tmp_path / "metrics_token"
    secret.write_text("\n", encoding="utf-8")

    with pytest.raises(ValidationError, match="is empty"):
        offline_settings(metrics_token=None, metrics_token_file=secret)


def test_short_metrics_token_is_rejected() -> None:
    with pytest.raises(ValidationError, match="at least 16 characters"):
        offline_settings(metrics_token="short")


def test_secrets_do_not_appear_in_repr() -> None:
    settings = offline_settings(db_password="db-password-value")

    rendered = repr(settings)

    assert "db-password-value" not in rendered
    assert METRICS_TOKEN not in rendered


def test_settings_come_from_prefixed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIOPS_REDIS_PORT", "6390")
    monkeypatch.setenv("AIOPS_LOG_LEVEL", "WARNING")
    monkeypatch.setenv("AIOPS_METRICS_TOKEN", METRICS_TOKEN)
    monkeypatch.delenv("AIOPS_METRICS_TOKEN_FILE", raising=False)

    settings = Settings(db_password=SecretStr("unused"), db_password_file=None)

    assert settings.log_level == "WARNING"
    assert settings.redis_port == 6390
    assert settings.metrics_token.get_secret_value() == METRICS_TOKEN
