"""Signed webhook requests to n8n.

The body is `{"payload": "<JSON text>"}`, and the Authorization header carries an HS256
JSON Web Token (RFC 7519) whose `payload_sha256` claim is the SHA-256 of that text.
n8n's Webhook node checks the token's signature and expiry against a JWT credential
(encrypted in n8n's database); the workflow's first Code node checks the hash. The
payload travels as a string so the hash covers the exact bytes that were signed, not
n8n's re-serialisation of parsed JSON.

The token is built here rather than with a JWT library: HS256 signing is one HMAC over
two base64url segments, and the tests check the result with PyJWT, and the end-to-end
test with n8n's own verifier (jsonwebtoken).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import SecretStr

ISSUER = "aiops-engine"
AUDIENCE = "aiops-n8n"
_HEADER = {"alg": "HS256", "typ": "JWT"}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _compact(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, allow_nan=False).encode()


def payload_text(payload: Mapping[str, Any]) -> str:
    """The payload as sent: compact, sorted keys, non-ASCII kept as is (UTF-8)."""
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False
    )


@dataclass(frozen=True)
class SignedRequest:
    body: bytes
    token: str


class Signer:
    def __init__(
        self,
        secret: SecretStr,
        *,
        ttl_seconds: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._key = secret.get_secret_value().encode()
        self._ttl_seconds = ttl_seconds
        self._clock = clock

    def sign(self, payload: Mapping[str, Any], *, subject: str, token_id: str) -> SignedRequest:
        """Body and bearer token for one webhook call.

        `subject` is the incident and `token_id` (jti) the notification: n8n checks both
        against the payload, so a token cannot be reused for another message.
        """
        text = payload_text(payload)
        issued = int(self._clock().timestamp())
        claims = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": subject,
            "jti": token_id,
            "iat": issued,
            "exp": issued + self._ttl_seconds,
            "payload_sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
        signing_input = f"{_b64url(_compact(_HEADER))}.{_b64url(_compact(claims))}"
        signature = hmac.new(self._key, signing_input.encode("ascii"), hashlib.sha256).digest()
        return SignedRequest(
            body=json.dumps({"payload": text}).encode("ascii"),
            token=f"{signing_input}.{_b64url(signature)}",
        )
