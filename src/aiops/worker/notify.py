"""Notification stage: hand each incident to n8n, once per message kind.

Two messages per incident at most: the firing message once the analysis is done (or has
failed), and the all-clear when the alert resolves. When the alert resolves before the
analysis is done, the firing message reports the resolution and no all-clear follows.

Which of the two applies is decided under a lock on the incident row, the same row the
ingestion updates when the alert resolves; so the two handlers cannot both decide that
the other one will report the resolution.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aiops.incidents.models import (
    EventKind,
    Incident,
    IncidentEvent,
    IncidentStatus,
    Notification,
    NotificationKind,
    NotificationStatus,
)
from aiops.incidents.topics import TOPIC_ANALYZED, TOPIC_RESOLVED
from aiops.notifications.client import N8nClient, NotifierRejectedError, NotifierUnavailableError
from aiops.notifications.payload import notification_payload
from aiops.worker.handlers import PermanentError, incident_id_of
from aiops.worker.metrics import WorkerMetrics

logger = logging.getLogger(__name__)

TOPIC_KINDS = {TOPIC_ANALYZED: NotificationKind.FIRING, TOPIC_RESOLVED: NotificationKind.RESOLVED}


class NotifyIncident:
    """incident.analyzed or incident.resolved -> a signed webhook to n8n."""

    def __init__(
        self,
        topic: str,
        sessions: async_sessionmaker[AsyncSession],
        client: N8nClient | None,
        metrics: WorkerMetrics,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.topic = topic
        self._kind = TOPIC_KINDS[topic]
        self._sessions = sessions
        self._client = client
        self._metrics = metrics
        self._clock = clock

    async def handle(self, payload: Mapping[str, Any]) -> None:
        incident_id = incident_id_of(payload)
        if self._client is None:
            return  # notifications are not configured
        prepared = await self._prepare(incident_id)
        if prepared is None:
            return
        notification_id, body = prepared
        await self._hand_off(self._client, incident_id, notification_id, body)

    async def on_exhausted(self, payload: Mapping[str, Any], reason: str) -> None:
        incident_id = incident_id_of(payload)
        async with self._sessions() as session:
            notification_id = await session.scalar(
                select(Notification.id).where(
                    Notification.incident_id == incident_id,
                    Notification.kind == self._kind,
                    Notification.handed_off_at.is_(None),
                )
            )
        if notification_id is not None:
            await self._fail(incident_id, notification_id, reason)

    async def _prepare(self, incident_id: uuid.UUID) -> tuple[uuid.UUID, dict[str, Any]] | None:
        """The notification to send now, with its payload, or None if there is none."""
        async with self._sessions() as session, session.begin():
            incident = await session.get(Incident, incident_id, with_for_update=True)
            if incident is None:
                msg = f"incident {incident_id} does not exist"
                raise PermanentError(msg)
            existing = await self._notification(session, incident_id, self._kind)
            if existing is not None:
                if existing.status == NotificationStatus.PENDING and existing.handed_off_at is None:
                    # Recorded, but n8n never accepted it: a retry, or a worker that died.
                    return existing.id, notification_payload(incident, existing)
                return None  # a redelivery of work already done
            if self._kind == NotificationKind.RESOLVED:
                firing = await self._notification(session, incident_id, NotificationKind.FIRING)
                if firing is None:
                    return None  # the firing message will report the resolution
                if firing.reported_status == IncidentStatus.RESOLVED:
                    return None  # it already did
            notification = Notification(
                id=uuid.uuid4(),
                incident_id=incident_id,
                kind=self._kind,
                status=NotificationStatus.PENDING,
                reported_status=incident.status,
                created_at=self._clock(),
                handoff_attempts=0,
            )
            session.add(notification)
            return notification.id, notification_payload(incident, notification)

    @staticmethod
    async def _notification(
        session: AsyncSession, incident_id: uuid.UUID, kind: NotificationKind
    ) -> Notification | None:
        return await session.scalar(
            select(Notification).where(
                Notification.incident_id == incident_id, Notification.kind == kind
            )
        )

    async def _hand_off(
        self,
        client: N8nClient,
        incident_id: uuid.UUID,
        notification_id: uuid.UUID,
        body: dict[str, Any],
    ) -> None:
        started = time.perf_counter()
        try:
            attempts = await client.deliver(
                body, incident_id=str(incident_id), notification_id=str(notification_id)
            )
        except NotifierUnavailableError as exc:
            self._observe("unavailable", started)
            await self._count_attempts(notification_id, exc.attempts)
            raise  # retried as a stream message; on_exhausted records the failure
        except NotifierRejectedError as exc:
            self._observe("rejected", started)
            await self._count_attempts(notification_id, exc.attempts)
            await self._fail(incident_id, notification_id, str(exc))
            return
        self._observe("accepted", started)
        await self._handed_off(incident_id, notification_id, attempts)

    def _observe(self, outcome: str, started: float) -> None:
        self._metrics.handoffs.labels(self._kind.value, outcome).inc()
        self._metrics.handoff_seconds.labels(outcome).observe(time.perf_counter() - started)

    async def _count_attempts(self, notification_id: uuid.UUID, attempts: int) -> None:
        async with self._sessions() as session, session.begin():
            await session.execute(
                update(Notification)
                .where(Notification.id == notification_id)
                .values(handoff_attempts=Notification.handoff_attempts + attempts)
            )

    async def _handed_off(
        self, incident_id: uuid.UUID, notification_id: uuid.UUID, attempts: int
    ) -> None:
        async with self._sessions() as session, session.begin():
            # n8n may already have claimed it (its status then is "processing"): only the
            # hand-off time and the attempts are the worker's to record.
            changed = await session.scalar(
                update(Notification)
                .where(Notification.id == notification_id, Notification.handed_off_at.is_(None))
                .values(
                    handed_off_at=self._clock(),
                    handoff_attempts=Notification.handoff_attempts + attempts,
                )
                .returning(Notification.id)
            )
            if changed is None:
                return
            session.add(
                IncidentEvent(
                    incident_id=incident_id,
                    kind=EventKind.NOTIFICATION_SENT,
                    details={
                        "notification_id": str(notification_id),
                        "kind": self._kind.value,
                        "attempts": attempts,
                    },
                )
            )
        logger.info(
            "notification handed to n8n",
            extra={"incident_id": str(incident_id), "kind": self._kind.value},
        )

    async def _fail(self, incident_id: uuid.UUID, notification_id: uuid.UUID, reason: str) -> None:
        async with self._sessions() as session, session.begin():
            changed = await session.scalar(
                update(Notification)
                .where(
                    Notification.id == notification_id,
                    Notification.status == NotificationStatus.PENDING,
                )
                .values(status=NotificationStatus.FAILED, completed_at=self._clock(), error=reason)
                .returning(Notification.id)
            )
            if changed is None:
                return
            session.add(
                IncidentEvent(
                    incident_id=incident_id,
                    kind=EventKind.NOTIFICATION_FAILED,
                    details={
                        "notification_id": str(notification_id),
                        "kind": self._kind.value,
                        "reason": reason,
                    },
                )
            )
        logger.error(
            "notification failed",
            extra={"incident_id": str(incident_id), "kind": self._kind.value, "reason": reason},
        )
