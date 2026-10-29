from __future__ import annotations

import httpx
import pytest

from aiops import __version__
from tests.conftest import METRICS_TOKEN

AUTH = {"Authorization": f"Bearer {METRICS_TOKEN}"}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token-0123456789"},
        {"Authorization": f"Basic {METRICS_TOKEN}"},
        {"Authorization": METRICS_TOKEN},
    ],
)
async def test_metrics_need_the_bearer_token(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> None:
    response = await client.get("/metrics", headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Bearer realm="metrics"'


async def test_metrics_label_requests_by_route_template(client: httpx.AsyncClient) -> None:
    await client.get("/health/live")
    await client.get("/health/live")
    await client.get("/definitely/not/a/route")

    response = await client.get("/metrics", headers=AUTH)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    text = response.text
    assert 'aiops_http_requests_total{method="GET",route="/health/live",status="200"} 2.0' in text
    assert 'aiops_http_requests_total{method="GET",route="unmatched",status="404"} 1.0' in text
    assert "/definitely/not/a/route" not in text  # raw paths never become labels
    build_info = next(line for line in text.splitlines() if line.startswith("aiops_build_info{"))
    assert 'python="3.12.' in build_info
    assert f'version="{__version__}"' in build_info
    assert "process_resident_memory_bytes" in text


async def test_metrics_endpoint_is_not_in_the_openapi_schema(client: httpx.AsyncClient) -> None:
    schema = (await client.get("/openapi.json")).json()

    assert "/metrics" not in schema["paths"]
    assert {"/health/live", "/health/ready"} <= set(schema["paths"])
