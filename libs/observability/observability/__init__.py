"""Shared telemetry for testbed services. HTTP client helpers live in `observability.http`."""

import os

from fastapi import FastAPI
from prometheus_client import REGISTRY

from observability.cgroup import register_cgroup_collector

from observability.context import get_request_id
from observability.logging import setup_logging
from observability.metrics import BUILD_INFO, metrics_endpoint, track
from observability.middleware import ObservabilityMiddleware
from observability.tracing import annotate_span, setup_tracing

__all__ = ["annotate_span", "get_request_id", "instrument", "track"]


def instrument(app: FastAPI, service: str, log_level: str = "INFO") -> None:
    """JSON logging, request-ID middleware with access logs and RED metrics, GET /metrics, OTel tracing.

    The release version (env SERVICE_VERSION) is on every log line, span resource and `app_build_info`.
    """
    version = os.environ.get("SERVICE_VERSION", "1.0.0")
    setup_logging(service, log_level, version)
    BUILD_INFO.labels(version).set(1)
    app.add_middleware(ObservabilityMiddleware)
    app.add_route("/metrics", metrics_endpoint, include_in_schema=False)
    register_cgroup_collector(REGISTRY)
    # The OTel ASGI middleware wraps the whole stack, so access logs run inside the server span.
    setup_tracing(app, service, version)
