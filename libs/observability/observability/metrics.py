import time
from contextlib import asynccontextmanager

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.requests import Request
from starlette.responses import Response

# The service name comes from the Prometheus `job` label, so it is not repeated here.
# Fine low end: in-network calls take ~1-25ms, and coarse buckets make distinct services look identical.
# Up to 30 s: injected latency goes that high (faultpoint), and so does nginx's proxy_read_timeout.
BUCKETS = (0.001, 0.0025, 0.005, 0.0075, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 10.0, 15.0,
           20.0, 30.0)

HTTP_REQUESTS = Counter(
    "http_requests_total", "Inbound HTTP requests", ["method", "route", "status"]
)
HTTP_DURATION = Histogram(
    "http_request_duration_seconds", "Inbound HTTP request duration", ["method", "route"], buckets=BUCKETS
)
BUILD_INFO = Gauge("app_build_info", "Running release of the service (always 1)", ["version"])
DEPENDENCY_REQUESTS = Counter(
    "dependency_requests_total", "Outbound calls to dependencies", ["dependency", "operation", "outcome"]
)
DEPENDENCY_DURATION = Histogram(
    "dependency_request_duration_seconds", "Outbound call duration", ["dependency", "operation"], buckets=BUCKETS
)


def is_timeout(exc: BaseException) -> bool:
    """Builtin/asyncio timeouts and client libraries' own ones (e.g. redis.exceptions.TimeoutError)."""
    return isinstance(exc, TimeoutError) or type(exc).__name__ == "TimeoutError"


def observe_dependency(dependency: str, operation: str, outcome: str, seconds: float) -> None:
    DEPENDENCY_REQUESTS.labels(dependency, operation, outcome).inc()
    DEPENDENCY_DURATION.labels(dependency, operation).observe(seconds)


@asynccontextmanager
async def track(dependency: str, operation: str):
    """Records duration and outcome (success|timeout|error|cancelled) of a non-HTTP dependency call."""
    start = time.perf_counter()
    outcome = "success"
    try:
        yield
    except Exception as exc:
        outcome = "timeout" if is_timeout(exc) else "error"
        raise
    except BaseException:  # asyncio.CancelledError: the caller gave up (e.g. the client disconnected)
        outcome = "cancelled"
        raise
    finally:
        observe_dependency(dependency, operation, outcome, time.perf_counter() - start)


async def metrics_endpoint(request: Request) -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
