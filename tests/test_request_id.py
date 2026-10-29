from __future__ import annotations

import re

import httpx
import pytest


async def test_request_id_is_generated_when_absent(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/live")

    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["x-request-id"])


async def test_valid_request_id_is_propagated(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/live", headers={"X-Request-ID": "alertmanager-4711"})

    assert response.headers["x-request-id"] == "alertmanager-4711"


@pytest.mark.parametrize(
    "unsafe",
    ["short", "has spaces in it", "x" * 129, "line\tbreak-injection"],
)
async def test_unsafe_request_id_is_replaced(client: httpx.AsyncClient, unsafe: str) -> None:
    response = await client.get("/health/live", headers={"X-Request-ID": unsafe})

    assert response.headers["x-request-id"] != unsafe
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["x-request-id"])
