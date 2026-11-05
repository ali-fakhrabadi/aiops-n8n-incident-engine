"""The webhook tokens verify with a standard JWT library and cover the exact payload."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import pytest
from pydantic import SecretStr, ValidationError

from aiops.notifications.signing import AUDIENCE, ISSUER, SignedRequest, Signer, payload_text
from tests.conftest import worker_settings

SECRET = "test-webhook-secret-0123456789abcdef"
PAYLOAD = {
    "version": 1,
    "notification": {"id": "n-1", "kind": "firing"},
    "incident": {"id": "i-1", "alertname": "DiskFull", "summary": "Platte voll: 95 % — /var"},
}


def _signed(issued: datetime | None = None) -> SignedRequest:
    when = issued or datetime.now(UTC)
    signer = Signer(SecretStr(SECRET), ttl_seconds=300, clock=lambda: when)
    return signer.sign(PAYLOAD, subject="i-1", token_id="n-1")


def _decode(token: str, key: str = SECRET) -> dict[str, Any]:
    claims: dict[str, Any] = jwt.decode(
        token, key, algorithms=["HS256"], audience=AUDIENCE, issuer=ISSUER
    )
    return claims


def test_the_token_verifies_with_a_standard_jwt_library() -> None:
    claims = _decode(_signed().token)

    assert claims["sub"] == "i-1"
    assert claims["jti"] == "n-1"
    assert claims["exp"] - claims["iat"] == 300
    assert jwt.get_unverified_header(_signed().token) == {"alg": "HS256", "typ": "JWT"}


def test_the_token_covers_the_exact_payload_text() -> None:
    signed = _signed()
    text = json.loads(signed.body)["payload"]

    assert hashlib.sha256(text.encode()).hexdigest() == _decode(signed.token)["payload_sha256"]
    assert json.loads(text) == PAYLOAD


def test_the_body_is_ascii_but_the_payload_keeps_its_characters() -> None:
    signed = _signed()

    signed.body.decode("ascii")
    assert "Platte voll: 95 % — /var" in json.loads(signed.body)["payload"]


def test_another_key_or_changed_claims_fail_verification() -> None:
    token = _signed().token
    header, claims, signature = token.split(".")
    changed = json.loads(base64.urlsafe_b64decode(claims + "=="))
    changed["jti"] = "n-2"
    forged = base64.urlsafe_b64encode(json.dumps(changed).encode()).rstrip(b"=").decode()

    with pytest.raises(jwt.InvalidSignatureError):
        _decode(token, key="another-secret-0123456789abcdefghij")
    with pytest.raises(jwt.InvalidSignatureError):
        _decode(f"{header}.{forged}.{signature}")


def test_a_token_expires_after_its_lifetime() -> None:
    with pytest.raises(jwt.ExpiredSignatureError):
        _decode(_signed(datetime.now(UTC) - timedelta(minutes=10)).token)


def test_the_payload_text_is_deterministic() -> None:
    assert payload_text({"b": 1, "a": [2, 1]}) == '{"a":[2,1],"b":1}'
    with pytest.raises(ValueError, match="not JSON compliant"):
        payload_text({"x": float("nan")})


def test_a_webhook_url_needs_a_long_enough_secret(tmp_path: Path) -> None:
    url = "http://n8n:5678/webhook/aiops-incident"
    with pytest.raises(ValidationError, match="needs n8n_webhook_secret"):
        worker_settings(tmp_path, n8n_webhook_url=url)
    with pytest.raises(ValidationError, match="at least 32 characters"):
        worker_settings(tmp_path, n8n_webhook_url=url, n8n_webhook_secret="short-secret")

    settings = worker_settings(tmp_path, n8n_webhook_url=url, n8n_webhook_secret=SECRET)
    assert settings.n8n_webhook_secret is not None
    assert SECRET not in repr(settings)
