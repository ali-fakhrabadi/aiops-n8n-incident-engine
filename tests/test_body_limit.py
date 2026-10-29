from __future__ import annotations

from collections.abc import AsyncIterator

from aiops.errors import PROBLEM_MEDIA_TYPE
from tests.conftest import offline_settings, serve

URL = "/api/v1/alerts/alertmanager"
LIMIT = 1024


async def test_a_declared_oversized_body_is_refused_before_reading(
    ingest_headers: dict[str, str],
) -> None:
    async with serve(offline_settings(max_request_bytes=LIMIT)) as client:
        response = await client.post(
            URL,
            content=b"x" * (LIMIT + 1),
            headers={**ingest_headers, "Content-Type": "application/json"},
        )

    assert response.status_code == 413
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert response.json()["detail"] == f"The request body is larger than {LIMIT} bytes."
    assert response.json()["request_id"] == response.headers["x-request-id"]


async def test_a_chunked_body_cannot_bypass_the_limit(ingest_headers: dict[str, str]) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(4):
            yield b"x" * (LIMIT // 2)

    async with serve(offline_settings(max_request_bytes=LIMIT)) as client:
        response = await client.post(
            URL, content=chunks(), headers={**ingest_headers, "Content-Type": "application/json"}
        )

    assert "content-length" not in {key.lower() for key in response.request.headers}
    assert response.status_code == 413
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
