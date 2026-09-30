import json
import logging
import sys
from datetime import UTC, datetime

from observability.context import get_request_id
from observability.tracing import current_trace_ids

# Attributes every LogRecord has; anything else on a record came from `extra=` and is emitted as a field.
_STANDARD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str, version: str):
        super().__init__()
        self.service = service
        self.version = version

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "service": self.service,
            "version": self.version,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        request_id = get_request_id()
        if request_id:
            payload["request_id"] = request_id
        trace_ids = current_trace_ids()
        if trace_ids:
            payload["trace_id"], payload["span_id"] = trace_ids
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(service: str, level: str = "INFO", version: str = "1.0.0") -> None:
    """Routes all logging (including uvicorn's) to stdout as one JSON object per line."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service, version))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
    # Access lines are emitted by ObservabilityMiddleware instead.
    logging.getLogger("uvicorn.access").disabled = True
    # httpx logs every request at INFO; InstrumentedTransport already covers outbound calls.
    logging.getLogger("httpx").setLevel(logging.WARNING)
