"""Hand-off to n8n: what is retried, with which back-off, and what is not."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
import respx
from pydantic import SecretStr

from aiops.notifications.client import (
    N8nClient,
    NotifierRejectedError,
    NotifierUnavailableError,
    RetryPolicy,
)
from aiops.notifications.signing import AUDIENCE, Signer

URL = "http://n8n.test:5678/webhook/aiops-incident"
SECRET = "test-webhook-secret-0123456789abcdef"
NOT_REGISTERED = {
    "code": 404,
    "message": 'The requested webhook "POST aiops-incident" is not registered.',
}
INVALID_SIGNATURE = {"code": 403, "message": "invalid signature"}


class Clock:
    """Real time, one minute later at every reading (each attempt signs afresh)."""

    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        self.now += timedelta(minutes=1)
        return self.now


@pytest.fixture
async def make_client() -> AsyncIterator[Callable[[list[float]], N8nClient]]:
    http = httpx.AsyncClient()

    def make(sleeps: list[float]) -> N8nClient:
        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)

        return N8nClient(
            URL,
            Signer(SecretStr(SECRET), ttl_seconds=300, clock=Clock()),
            http,
            RetryPolicy(attempts=3, backoff_seconds=2.0),
            sleep=sleep,
        )

    yield make
    await http.aclose()


async def _deliver(client: N8nClient) -> int:
    return await client.deliver({"version": 1}, incident_id="i-1", notification_id="n-1")


def _token(request: httpx.Request) -> dict[str, object]:
    token = request.headers["Authorization"].removeprefix("Bearer ")
    # The clock runs ahead (a minute per attempt), so allow tokens issued "in the future".
    claims: dict[str, object] = jwt.decode(
        token, SECRET, algorithms=["HS256"], audience=AUDIENCE, leeway=600
    )
    return claims


@respx.mock
async def test_an_accepted_notification_takes_one_attempt(
    make_client: Callable[[list[float]], N8nClient],
) -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(202, json={"accepted": True}))
    sleeps: list[float] = []

    assert await _deliver(make_client(sleeps)) == 1

    request = route.calls.last.request
    assert request.headers["Content-Type"] == "application/json"
    assert _token(request)["jti"] == "n-1"
    assert sleeps == []


@respx.mock
async def test_unavailable_n8n_is_retried_with_exponential_backoff(
    make_client: Callable[[list[float]], N8nClient],
) -> None:
    route = respx.post(URL).mock(
        side_effect=[
            httpx.ConnectError("connection refused"),
            httpx.Response(503),
            httpx.Response(202),
        ]
    )
    sleeps: list[float] = []

    assert await _deliver(make_client(sleeps)) == 3

    assert sleeps == [2.0, 4.0]
    tokens = [call.request.headers["Authorization"] for call in route.calls]
    assert len(set(tokens)) == 3  # signed afresh each time


@respx.mock
async def test_after_the_last_attempt_the_error_says_why(
    make_client: Callable[[list[float]], N8nClient],
) -> None:
    respx.post(URL).mock(return_value=httpx.Response(404, json=NOT_REGISTERED))
    sleeps: list[float] = []

    with pytest.raises(NotifierUnavailableError, match="404: The requested webhook") as raised:
        await _deliver(make_client(sleeps))

    assert raised.value.attempts == 3
    assert sleeps == [2.0, 4.0]


@pytest.mark.parametrize(
    ("response", "detail"),
    [
        (httpx.Response(403, json=INVALID_SIGNATURE), "invalid signature"),
        (httpx.Response(400, text="payload does not match the token"), "does not match"),
    ],
)
@respx.mock
async def test_a_refused_request_is_not_retried(
    make_client: Callable[[list[float]], N8nClient], response: httpx.Response, detail: str
) -> None:
    respx.post(URL).mock(return_value=response)
    sleeps: list[float] = []

    with pytest.raises(NotifierRejectedError, match=detail) as raised:
        await _deliver(make_client(sleeps))

    assert raised.value.attempts == 1
    assert sleeps == []
