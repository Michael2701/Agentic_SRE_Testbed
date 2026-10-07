import logging
import time

from observability.context import REQUEST_ID_HEADER, accept_or_generate, request_id_var
from observability.metrics import HTTP_DURATION, HTTP_REQUESTS

logger = logging.getLogger("observability.access")

# Probe and scrape traffic would drown the signal in logs and RED metrics.
UNOBSERVED_PATHS = frozenset({"/health", "/ready", "/metrics"})
# Control-plane endpoints (fault injection). They must stay invisible to the diagnostic plane.
CONTROL_PLANE_PREFIX = "/__"
# Anything else becomes OTHER, so clients can't create label values (series) at will.
KNOWN_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})


def is_unobserved(path: str) -> bool:
    return path in UNOBSERVED_PATHS or path.startswith(CONTROL_PLANE_PREFIX)


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
        started = False
        start = time.perf_counter()

        async def send_with_request_id(message):
            nonlocal status, started
            if message["type"] == "http.response.start":
                status, started = message["status"], True
                message["headers"] = [
                    *message.get("headers", []),
                    (REQUEST_ID_HEADER.lower().encode(), request_id.encode()),
                ]
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            logger.exception("unhandled_exception")
            if started:  # too late for an error response; the status already sent is what the client saw
                raise
            # Answered here rather than by Starlette's outer error middleware, so it carries the request ID.
            status = 500
            await send_with_request_id({"type": "http.response.start", "status": 500,
                                        "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
            await send({"type": "http.response.body", "body": b"Internal Server Error"})
        finally:
            if not is_unobserved(scope["path"]):
                self._record(scope, status, time.perf_counter() - start)
            request_id_var.reset(token)

    @staticmethod
    def _record(scope, status: int, seconds: float) -> None:
        route = scope.get("route")
        route_path = getattr(route, "path", "unmatched")
        method = scope["method"] if scope["method"] in KNOWN_METHODS else "OTHER"
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
