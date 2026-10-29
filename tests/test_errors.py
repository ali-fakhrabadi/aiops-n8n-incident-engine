from __future__ import annotations

import httpx
from fastapi import FastAPI

from aiops.errors import PROBLEM_MEDIA_TYPE
from tests.conftest import offline_settings, serve


async def test_unknown_route_is_a_problem_document(client: httpx.AsyncClient) -> None:
    response = await client.get("/no/such/route")

    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body = response.json()
    assert body["type"] == "about:blank"
    assert body["title"] == "Not Found"
    assert body["status"] == 404
    assert body["instance"] == "/no/such/route"
    assert body["request_id"] == response.headers["x-request-id"]
    assert "detail" not in body  # Starlette's default detail only repeated the title


async def test_wrong_method_is_405_with_allow_header(client: httpx.AsyncClient) -> None:
    response = await client.post("/health/live")

    assert response.status_code == 405
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert "GET" in response.headers["allow"]


def _add_typed_route(app: FastAPI) -> None:
    async def echo(count: int) -> dict[str, int]:
        return {"count": count}

    app.add_api_route("/echo", echo, methods=["GET"])


async def test_validation_errors_do_not_echo_the_input() -> None:
    async with serve(offline_settings(), _add_typed_route) as client:
        response = await client.get("/echo", params={"count": "secret-looking-value"})

    assert response.status_code == 422
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body = response.json()
    assert body["detail"] == "The request is invalid."
    assert body["errors"] == [
        {
            "loc": ["query", "count"],
            "msg": "Input should be a valid integer, unable to parse string as an integer",
            "type": "int_parsing",
        }
    ]
    assert "secret-looking-value" not in response.text


def _add_failing_route(app: FastAPI) -> None:
    async def explode() -> None:
        msg = "database password is hunter2"
        raise RuntimeError(msg)

    app.add_api_route("/explode", explode, methods=["GET"])


async def test_unhandled_error_is_generic_and_carries_the_request_id() -> None:
    async with serve(offline_settings(), _add_failing_route) as client:
        response = await client.get("/explode", headers={"X-Request-ID": "trace-12345678"})

    assert response.status_code == 500
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert response.headers["x-request-id"] == "trace-12345678"
    body = response.json()
    assert body["detail"] == "An unexpected error occurred."
    assert body["request_id"] == "trace-12345678"
    assert "hunter2" not in response.text
