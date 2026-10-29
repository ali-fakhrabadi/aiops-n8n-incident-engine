"""Request ID, access log, metrics and last-resort error handling in one ASGI layer.

A plain ASGI middleware rather than BaseHTTPMiddleware: it does not buffer streaming
responses and runs in the same task as the endpoint, so the request ID in the context
variable is visible to every log line and error response of that request.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from http import HTTPStatus

from fastapi import HTTPException
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from aiops.context import request_id_var
from aiops.errors import problem_response
from aiops.metrics import UNMATCHED_ROUTE, HttpMetrics

REQUEST_ID_HEADER = "X-Request-ID"
# Accept a caller's request ID only if it is a plain token of sane length.
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{8,128}")
# Probes and scrapes are logged at DEBUG unless they fail.
_QUIET_ROUTES = frozenset({"/health/live", "/health/ready", "/metrics"})

access_logger = logging.getLogger("aiops.access")
error_logger = logging.getLogger("aiops.errors")


def _incoming_request_id(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == b"x-request-id":
            candidate = value.decode("latin-1")
            return candidate if _VALID_REQUEST_ID.fullmatch(candidate) else None
    return None


def _route_template(scope: Scope) -> str:
    # FastAPI stores the matched route in the scope; its path is the template.
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else UNMATCHED_ROUTE


class BodySizeLimitMiddleware:
    """Reject request bodies over `max_bytes` with 413, declared or not.

    A Content-Length over the limit is refused before reading; a chunked body is counted
    while it streams in, so a client cannot bypass the limit by omitting the header.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        for name, value in scope.get("headers", []):
            if name == b"content-length" and value.isdigit() and int(value) > self._max_bytes:
                await self._too_large()(scope, receive, send)
                return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max_bytes:
                    # An HTTPException, so FastAPI's body parsing re-raises it unchanged.
                    raise HTTPException(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, detail=self._detail)
            return message

        await self._app(scope, limited_receive, send)

    @property
    def _detail(self) -> str:
        return f"The request body is larger than {self._max_bytes} bytes."

    def _too_large(self) -> ASGIApp:
        return problem_response(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, detail=self._detail)


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, metrics: HttpMetrics) -> None:
        self._app = app
        self._metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope) or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        status = HTTPStatus.INTERNAL_SERVER_ERROR.value
        response_started = False
        started = time.perf_counter()

        async def send_with_request_id(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                status = message["status"]
                response_started = True
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
            await send(message)

        try:
            await self._app(scope, receive, send_with_request_id)
        except Exception:
            error_logger.exception("unhandled error", extra={"path": scope["path"]})
            if response_started:
                raise  # too late for an error response; let the server drop the connection
            # Generic on purpose: internals stay in the log, the request ID links them.
            response = problem_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                detail="An unexpected error occurred.",
                instance=scope["path"],
            )
            await response(scope, receive, send_with_request_id)
        finally:
            elapsed = time.perf_counter() - started
            route = _route_template(scope)
            self._metrics.observe(scope["method"], route, status, elapsed)
            quiet = route in _QUIET_ROUTES and status < HTTPStatus.BAD_REQUEST
            access_logger.log(
                logging.DEBUG if quiet else logging.INFO,
                "request completed",
                extra={
                    "method": scope["method"],
                    "route": route,
                    "path": scope["path"],
                    "status": status,
                    "duration_ms": round(elapsed * 1000, 2),
                },
            )
            request_id_var.reset(token)
