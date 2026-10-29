"""Bearer-token checks; each caller class has its own token.

Alertmanager can only deliver (ingest token), Prometheus can only scrape (metrics
token), and operators or n8n read incidents (API token). A leaked token exposes one
capability, and each can be rotated without touching the others.
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import SecretStr

from aiops.api.dependencies import get_settings
from aiops.config import Settings

_ingest = HTTPBearer(
    auto_error=False, scheme_name="IngestToken", description="Alertmanager's delivery token."
)
_api = HTTPBearer(
    auto_error=False, scheme_name="ApiToken", description="Token for reading incidents."
)
_metrics = HTTPBearer(auto_error=False, scheme_name="MetricsToken")

Credentials = HTTPAuthorizationCredentials | None


def check_bearer(credentials: Credentials, expected: SecretStr, realm: str) -> None:
    presented = credentials.credentials if credentials is not None else ""
    # Constant-time comparison: response timing must not reveal how much matched.
    if not presented or not hmac.compare_digest(
        presented.encode(), expected.get_secret_value().encode()
    ):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="A valid bearer token is required.",
            headers={"WWW-Authenticate": f'Bearer realm="{realm}"'},
        )


async def require_ingest_token(
    settings: Annotated[Settings, Depends(get_settings)],
    credentials: Annotated[Credentials, Depends(_ingest)],
) -> None:
    check_bearer(credentials, settings.ingest_token, "ingest")


async def require_api_token(
    settings: Annotated[Settings, Depends(get_settings)],
    credentials: Annotated[Credentials, Depends(_api)],
) -> None:
    check_bearer(credentials, settings.api_token, "api")


async def require_metrics_token(
    settings: Annotated[Settings, Depends(get_settings)],
    credentials: Annotated[Credentials, Depends(_metrics)],
) -> None:
    check_bearer(credentials, settings.metrics_token, "metrics")
