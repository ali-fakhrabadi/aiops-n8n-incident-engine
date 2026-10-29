"""Alertmanager webhook receiver."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aiops.api.auth import require_ingest_token
from aiops.api.dependencies import get_session_factory, get_settings
from aiops.config import Settings
from aiops.incidents.alertmanager import AlertmanagerWebhook
from aiops.incidents.ingest import IngestPolicy, ingest
from aiops.incidents.schemas import IngestResponse

router = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])
logger = logging.getLogger(__name__)


@router.post(
    "/alertmanager",
    summary="Receive an Alertmanager webhook delivery",
    dependencies=[Depends(require_ingest_token)],
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Missing or wrong ingest token"},
        HTTPStatus.REQUEST_ENTITY_TOO_LARGE: {"description": "Body or alert count over the limit"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Database unavailable; retry"},
    },
)
async def receive_alertmanager(
    webhook: AlertmanagerWebhook,
    settings: Annotated[Settings, Depends(get_settings)],
    sessions: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> IngestResponse:
    """Idempotent: Alertmanager may deliver the same alerts more than once.

    Any 2xx tells Alertmanager the delivery is done; 5xx makes it retry, so a database
    outage delays alerts instead of losing them.
    """
    if len(webhook.alerts) > settings.max_alerts_per_request:
        raise HTTPException(
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            detail=f"At most {settings.max_alerts_per_request} alerts per request.",
        )
    policy = IngestPolicy(
        flap_threshold=settings.flap_threshold,
        flap_window=timedelta(minutes=settings.flap_window_minutes),
    )
    try:
        async with sessions() as session, session.begin():
            result = await ingest(session, webhook, policy=policy, now=datetime.now(UTC))
    except (DBAPIError, OSError, TimeoutError) as exc:
        logger.warning("alert ingestion failed", extra={"error": type(exc).__name__})
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="The database is unavailable; retry."
        ) from exc
    logger.info(
        "alerts ingested",
        extra={
            "receiver": webhook.receiver,
            "group_status": webhook.status,
            "received": result.received,
            "opened": result.opened,
            "resolved": result.resolved,
            "ignored": result.ignored,
        },
    )
    return IngestResponse(**vars(result))
