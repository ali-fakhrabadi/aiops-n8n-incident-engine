"""The worker's side of the webhook to n8n."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from aiops.notifications.signing import Signer

logger = logging.getLogger(__name__)

# n8n answers 404 while the workflow is not published yet (or n8n is still starting),
# which a later attempt can outlive; so can a timeout, a rate limit or a server error.
_RETRYABLE = frozenset({404, 408, 425, 429})
_SERVER_ERROR = 500
_DETAIL_LENGTH = 300


class HandoffError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.attempts = 0


class NotifierUnavailableError(HandoffError):
    """n8n is unreachable or not ready: try again later."""


class NotifierRejectedError(HandoffError):
    """n8n refused the request (signature, payload): retrying cannot help."""


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int
    backoff_seconds: float

    def delay(self, attempt: int) -> float:
        """Exponential: backoff, 2 x backoff, 4 x backoff, ..."""
        return self.backoff_seconds * 2.0 ** (attempt - 1)


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        text = response.text
    else:
        text = str(body.get("message", body)) if isinstance(body, dict) else str(body)
    return text.strip()[:_DETAIL_LENGTH]


class N8nClient:
    def __init__(
        self,
        url: str,
        signer: Signer,
        http: httpx.AsyncClient,
        retry: RetryPolicy,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._url = url
        self._signer = signer
        self._http = http
        self._retry = retry
        self._sleep = sleep

    async def deliver(
        self, payload: Mapping[str, Any], *, incident_id: str, notification_id: str
    ) -> int:
        """Hand one notification to n8n; returns the attempts it took.

        Every attempt is signed afresh, so a long back-off cannot outlast the token.
        The errors raised carry the attempts made as `attempts`.
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                await self._post(payload, incident_id, notification_id)
            except NotifierUnavailableError as exc:
                exc.attempts = attempt
                if attempt >= self._retry.attempts:
                    raise
                logger.warning(
                    "n8n unavailable, retrying",
                    extra={"incident_id": incident_id, "attempt": attempt, "error": str(exc)},
                )
                await self._sleep(self._retry.delay(attempt))
            except NotifierRejectedError as exc:
                exc.attempts = attempt
                raise
            else:
                return attempt

    async def _post(
        self, payload: Mapping[str, Any], incident_id: str, notification_id: str
    ) -> None:
        signed = self._signer.sign(payload, subject=incident_id, token_id=notification_id)
        try:
            response = await self._http.post(
                self._url,
                content=signed.body,
                headers={
                    "Authorization": f"Bearer {signed.token}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.TransportError as exc:
            msg = f"n8n is unreachable: {type(exc).__name__}"
            raise NotifierUnavailableError(msg) from exc
        if response.is_success:
            return
        message = f"n8n answered {response.status_code}: {_detail(response)}"
        if response.status_code in _RETRYABLE or response.status_code >= _SERVER_ERROR:
            raise NotifierUnavailableError(message)
        raise NotifierRejectedError(message)
