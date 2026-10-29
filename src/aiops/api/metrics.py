"""Prometheus scrape endpoint, protected by its own bearer token."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest

from aiops.api.auth import require_metrics_token
from aiops.api.dependencies import get_registry

router = APIRouter()


@router.get("/metrics", include_in_schema=False, dependencies=[Depends(require_metrics_token)])
async def metrics(registry: Annotated[CollectorRegistry, Depends(get_registry)]) -> Response:
    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
