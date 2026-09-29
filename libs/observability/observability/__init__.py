"""Shared telemetry for testbed services. HTTP client helpers live in `observability.http`."""

from fastapi import FastAPI

from observability.context import get_request_id
from observability.logging import setup_logging
from observability.metrics import metrics_endpoint, track
from observability.middleware import ObservabilityMiddleware

__all__ = ["get_request_id", "instrument", "track"]


def instrument(app: FastAPI, service: str, log_level: str = "INFO") -> None:
    """JSON logging, request-ID middleware with access logs and RED metrics, and GET /metrics."""
    setup_logging(service, log_level)
    app.add_middleware(ObservabilityMiddleware)
    app.add_route("/metrics", metrics_endpoint, include_in_schema=False)
