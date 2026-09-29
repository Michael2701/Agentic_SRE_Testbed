import logging
import time

from observability.context import REQUEST_ID_HEADER, accept_or_generate, request_id_var
from observability.metrics import HTTP_DURATION, HTTP_REQUESTS

logger = logging.getLogger("observability.access")

# Probe and scrape traffic would drown the signal in logs and RED metrics.
UNOBSERVED_PATHS = frozenset({"/health", "/ready", "/metrics"})


class ObservabilityMiddleware:
    """Pure ASGI middleware: request ID context, one access log line and RED metrics per request."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope["headers"]}
        request_id = accept_or_generate(headers.get(REQUEST_ID_HEADER.lower()))
        token = request_id_var.set(request_id)
        status = 500
        start = time.perf_counter()

        async def send_with_request_id(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"] = [
                    *message.get("headers", []),
                    (REQUEST_ID_HEADER.lower().encode(), request_id.encode()),
                ]
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            status = 500
            logger.exception("unhandled_exception")
            raise
        finally:
            if scope["path"] not in UNOBSERVED_PATHS:
                self._record(scope, status, time.perf_counter() - start)
            request_id_var.reset(token)

    @staticmethod
    def _record(scope, status: int, seconds: float) -> None:
        route = scope.get("route")
        route_path = getattr(route, "path", "unmatched")
        method = scope["method"]
        HTTP_REQUESTS.labels(method, route_path, str(status)).inc()
        HTTP_DURATION.labels(method, route_path).observe(seconds)
        level = logging.ERROR if status >= 500 else logging.INFO
        logger.log(
            level,
            "request",
            extra={
                "method": method,
                "route": route_path,
                "path": scope["path"],
                "status": status,
                "duration_ms": round(seconds * 1000, 2),
            },
        )
