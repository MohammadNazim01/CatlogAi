"""Request-ID + access-log middleware, written as pure ASGI.

(Starlette's BaseHTTPMiddleware breaks contextvar propagation and streaming, so we avoid it.)
"""

import logging
import re
import time
import uuid
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import request_id_var

logger = logging.getLogger("app.access")

REQUEST_ID_HEADER = "X-Request-ID"
# Only accept sane inbound IDs; otherwise a client could inject arbitrary text into our logs.
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")


def get_request_id(scope: Scope) -> str:
    return str(scope.get("state", {}).get("request_id", "-"))


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get(REQUEST_ID_HEADER)
        request_id = (
            incoming if incoming and _VALID_REQUEST_ID.fullmatch(incoming) else uuid.uuid4().hex
        )
        scope.setdefault("state", {})["request_id"] = request_id
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status_code = 500  # stays 500 if an unhandled exception escapes to ServerErrorMiddleware

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            path: str = scope["path"]  # never log the query string: it can carry tokens
            fields: dict[str, Any] = {
                "method": scope["method"],
                "path": path,
                "status": status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            }
            level = logging.DEBUG if path.startswith("/health") else logging.INFO
            logger.log(level, "request", extra=fields)
            request_id_var.reset(token)
