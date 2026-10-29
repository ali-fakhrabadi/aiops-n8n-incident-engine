"""Prometheus scrape endpoint, protected by a bearer token."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest

from aiops.api.dependencies import get_registry, get_settings
from aiops.config import Settings

router = APIRouter()


@router.get("/metrics", include_in_schema=False)
async def metrics(
    settings: Annotated[Settings, Depends(get_settings)],
    registry: Annotated[CollectorRegistry, Depends(get_registry)],
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    scheme, _, token = (authorization or "").partition(" ")
    expected = settings.metrics_token.get_secret_value()
    # Constant-time comparison: response timing must not reveal how much matched.
    if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="A valid bearer token is required.",
            headers={"WWW-Authenticate": 'Bearer realm="metrics"'},
        )
    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
