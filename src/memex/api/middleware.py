from __future__ import annotations

import time
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from memex.logging import bind_request_context, clear_request_context, get_logger

_log = get_logger("memex.api.http")

_REQUEST_ID_HEADER = "X-Request-ID"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Generates a per-request id, binds it to structlog contextvars, and logs
    one `http.request` event per response (success or error).

    user_id is bound separately by the auth dependency when it runs — endpoints
    without auth (e.g. /healthz) therefore log without a user_id, which is the
    correct behavior.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        incoming = request.headers.get(_REQUEST_ID_HEADER)
        request_id = incoming if incoming else uuid4().hex[:16]
        bind_request_context(request_id=request_id)

        started = time.perf_counter()
        client_ip = request.client.host if request.client else None

        try:
            response = await call_next(request)
        except Exception as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            _log.exception(
                "http.request.error",
                method=request.method,
                path=request.url.path,
                duration_ms=duration_ms,
                client_ip=client_ip,
                exc_type=type(exc).__name__,
                exc_msg=str(exc),
            )
            clear_request_context()
            raise

        duration_ms = int((time.perf_counter() - started) * 1000)
        response.headers[_REQUEST_ID_HEADER] = request_id
        _log.info(
            "http.request",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
            client_ip=client_ip,
        )
        clear_request_context()
        return response
