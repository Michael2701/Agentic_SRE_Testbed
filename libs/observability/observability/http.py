import logging
import re
import time

import httpx

from observability.context import REQUEST_ID_HEADER, get_request_id
from observability.metrics import observe_dependency

logger = logging.getLogger("observability.dependency")

# Collapse IDs in paths so the `operation` label stays low-cardinality.
_ID_SEGMENT = re.compile(r"/(?:[0-9a-fA-F-]{32,36}|\d+)(?=/|$)")


def _operation(request: httpx.Request) -> str:
    return f"{request.method} {_ID_SEGMENT.sub('/{id}', request.url.path)}"


def _name_client_span(span, request) -> None:
    """OTel httpx hook: names client spans "POST /payments" instead of the bare method."""
    if span is None or not span.is_recording():
        return
    method = request.method.decode() if isinstance(request.method, bytes) else request.method
    span.update_name(f"{method} {_ID_SEGMENT.sub('/{id}', request.url.path)}")


async def _name_client_span_async(span, request) -> None:
    _name_client_span(span, request)


OTEL_HOOKS = {"request_hook": _name_client_span, "async_request_hook": _name_client_span_async}


class InstrumentedTransport(httpx.AsyncBaseTransport):
    """Propagates X-Request-ID and records dependency metrics for every outbound call."""

    def __init__(self, dependency: str):
        self.dependency = dependency
        self._inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request_id = get_request_id()
        if request_id:
            request.headers[REQUEST_ID_HEADER] = request_id

        operation = _operation(request)
        outcome = "success"
        error = None
        start = time.perf_counter()
        try:
            response = await self._inner.handle_async_request(request)
            if response.status_code >= 500:
                outcome, error = "error", f"HTTP {response.status_code}"
            return response
        except httpx.TimeoutException as exc:
            outcome, error = "timeout", repr(exc)
            raise
        except Exception as exc:
            outcome, error = "error", repr(exc)
            raise
        finally:
            seconds = time.perf_counter() - start
            observe_dependency(self.dependency, operation, outcome, seconds)
            if outcome != "success":
                logger.warning(
                    "dependency_call_failed",
                    extra={
                        "dependency": self.dependency,
                        "operation": operation,
                        "outcome": outcome,
                        "error": error,
                        "duration_ms": round(seconds * 1000, 2),
                    },
                )

    async def aclose(self) -> None:
        await self._inner.aclose()


def instrumented_client(base_url: str, dependency: str, timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=InstrumentedTransport(dependency))
