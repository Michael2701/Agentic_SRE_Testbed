import re
import uuid
from contextvars import ContextVar

REQUEST_ID_HEADER = "X-Request-ID"
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def get_request_id() -> str | None:
    return request_id_var.get()


def accept_or_generate(value: str | None) -> str:
    """Keeps a well-formed incoming request ID, otherwise generates a new one."""
    if value and _VALID_REQUEST_ID.match(value):
        return value
    return uuid.uuid4().hex
