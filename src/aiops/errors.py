"""RFC 9457 (formerly 7807) problem details for every error response."""

from __future__ import annotations

from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from aiops.context import request_id_var

PROBLEM_MEDIA_TYPE = "application/problem+json"


def _phrase(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Error"


def problem_response(
    status: int,
    *,
    detail: str | None = None,
    instance: str | None = None,
    headers: Mapping[str, str] | None = None,
    extensions: Mapping[str, Any] | None = None,
) -> JSONResponse:
    """A problem+json response; carries the request ID so clients can quote it."""
    body: dict[str, Any] = {"type": "about:blank", "title": _phrase(status), "status": status}
    if detail:
        body["detail"] = detail
    if instance:
        body["instance"] = instance
    request_id = request_id_var.get()
    if request_id is not None:
        body["request_id"] = request_id
    if extensions:
        body.update(extensions)
    return JSONResponse(
        body, status_code=status, media_type=PROBLEM_MEDIA_TYPE, headers=dict(headers or {})
    )


async def _http_exception(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, StarletteHTTPException):  # registered for this type only
        raise exc
    # FastAPI's HTTPException allows any JSON detail; only a plain string is shown.
    detail: str | None = exc.detail if isinstance(exc.detail, str) else None
    if detail == _phrase(exc.status_code):
        detail = None  # Starlette's default detail only repeats the title
    return problem_response(
        exc.status_code, detail=detail, instance=request.url.path, headers=exc.headers
    )


async def _validation_error(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, RequestValidationError):  # registered for this type only
        raise exc
    # Location, message and type only: the rejected input is not echoed back, so a
    # misplaced secret in a request is never reflected into responses or logs.
    errors = [
        {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
        for error in exc.errors()
    ]
    return problem_response(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        detail="The request is invalid.",
        instance=request.url.path,
        extensions={"errors": errors},
    )


def install_problem_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, _http_exception)
    app.add_exception_handler(RequestValidationError, _validation_error)
