import atexit
import importlib
import logging

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased

logger = logging.getLogger("observability.tracing")

# Client libraries are instrumented only when the service installed the matching extra.
_LIBRARY_INSTRUMENTORS = (
    ("opentelemetry.instrumentation.httpx", "HTTPXClientInstrumentor"),
    ("opentelemetry.instrumentation.asyncpg", "AsyncPGInstrumentor"),
    ("opentelemetry.instrumentation.redis", "RedisInstrumentor"),
)

# Same paths the access log and RED metrics skip (regexes matched against the URL),
# including control-plane `/__*` endpoints.
EXCLUDED_URLS = "/health$,/ready$,/metrics$,/__"


def setup_tracing(app: FastAPI, service: str, version: str = "1.0.0") -> None:
    """Exports spans over OTLP/HTTP (endpoint from OTEL_EXPORTER_OTLP_ENDPOINT); samples everything."""
    provider = TracerProvider(
        resource=Resource.create({"service.name": service, "service.version": version}),
        sampler=ParentBased(ALWAYS_ON),
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    atexit.register(provider.shutdown)  # flushes buffered spans on SIGTERM, e.g. a restart fault

    for module_name, class_name in _LIBRARY_INSTRUMENTORS:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        getattr(module, class_name)().instrument(**_instrumentor_options(class_name))
        logger.debug("instrumented", extra={"instrumentor": class_name})

    # ASGI send/receive spans add noise without information for this testbed.
    FastAPIInstrumentor.instrument_app(app, excluded_urls=EXCLUDED_URLS, exclude_spans=["receive", "send"])


def _instrumentor_options(class_name: str) -> dict:
    if class_name == "HTTPXClientInstrumentor":
        from observability.http import OTEL_HOOKS

        return OTEL_HOOKS
    return {}


def annotate_span(attributes: dict) -> None:
    """Sets attributes on the current span (e.g. {"order.id": ...}); never pass secrets."""
    span = trace.get_current_span()
    for key, value in attributes.items():
        if value is not None:
            span.set_attribute(key, value)


def current_trace_ids() -> tuple[str, str] | None:
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None
    return format(context.trace_id, "032x"), format(context.span_id, "016x")
