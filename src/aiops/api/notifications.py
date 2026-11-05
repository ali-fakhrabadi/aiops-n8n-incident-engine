"""Delivery reports from n8n (callback token only)."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aiops.api.auth import require_callback_token
from aiops.api.dependencies import get_notification_metrics, get_session_factory
from aiops.incidents.models import NotificationStatus
from aiops.incidents.schemas import NotificationOut
from aiops.metrics import NotificationMetrics
from aiops.notifications.delivery import (
    DeliveryConflictError,
    DeliveryReport,
    Outcome,
    Recorded,
    UnknownNotificationError,
    record_report,
)

router = APIRouter(
    prefix="/api/v1/notifications",
    tags=["notifications"],
    dependencies=[Depends(require_callback_token)],
    responses={status.HTTP_401_UNAUTHORIZED: {"description": "Missing or wrong callback token"}},
)
logger = logging.getLogger(__name__)

# Short machine-readable names: n8n's route and channel, its execution IDs (digits).
_NAME = r"^[a-z0-9][a-z0-9_.-]*$"


class DeliveryReportIn(BaseModel):
    # Telegram message IDs and n8n execution IDs are numbers in some n8n expressions.
    model_config = ConfigDict(extra="forbid", coerce_numbers_to_str=True)

    status: Literal["processing", "delivered", "suppressed", "failed"] = Field(
        description="`processing` claims the notification before sending; then the outcome."
    )
    route: str | None = Field(default=None, max_length=32, pattern=_NAME)
    channel: str | None = Field(default=None, max_length=32, pattern=_NAME)
    channel_message_id: str | None = Field(default=None, max_length=64)
    error: str | None = Field(default=None, max_length=2000)
    execution_id: str | None = Field(default=None, max_length=64, pattern=_NAME)


@router.post(
    "/{notification_id}/delivery",
    summary="Claim a notification before sending it, then report its outcome",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "No such notification"},
        status.HTTP_409_CONFLICT: {
            "description": "Already claimed by another execution, or another outcome recorded"
        },
    },
)
async def report_delivery(
    notification_id: uuid.UUID,
    report: DeliveryReportIn,
    sessions: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    metrics: Annotated[NotificationMetrics, Depends(get_notification_metrics)],
) -> NotificationOut:
    """Idempotent: repeating a claim from the same execution, or the same outcome, is a
    no-op that returns the current state; so is any retry n8n makes."""
    try:
        async with sessions() as session, session.begin():
            recorded = await record_report(
                session,
                notification_id,
                DeliveryReport(
                    status=NotificationStatus(report.status),
                    route=report.route,
                    channel=report.channel,
                    channel_message_id=report.channel_message_id,
                    error=report.error,
                    execution_id=report.execution_id,
                ),
                datetime.now(UTC),
            )
            out = NotificationOut.model_validate(recorded.notification)
    except UnknownNotificationError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No such notification.") from exc
    except DeliveryConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"{exc}.") from exc
    if recorded.outcome == Outcome.COMPLETED:
        _observe(metrics, recorded)
    if recorded.outcome != Outcome.UNCHANGED:
        logger.info(
            "delivery reported",
            extra={
                "incident_id": str(recorded.incident.id),
                "notification_id": str(notification_id),
                "kind": out.kind.value,
                "status": out.status.value,
                "route": out.route,
            },
        )
    return out


def _observe(metrics: NotificationMetrics, recorded: Recorded) -> None:
    notification = recorded.notification
    metrics.outcomes.labels(
        notification.kind, notification.route or "none", notification.status
    ).inc()
    since_alert = recorded.seconds_since_alert()
    if notification.status == NotificationStatus.DELIVERED and since_alert is not None:
        metrics.alert_to_delivery.labels(notification.kind).observe(since_alert)
    in_channel = recorded.seconds_in_channel()
    if in_channel is not None:
        metrics.channel.labels(notification.channel or "none", notification.status).observe(
            in_channel
        )
