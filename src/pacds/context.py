"""Per-request context shared with code that has no access to the request object."""

from contextvars import ContextVar

request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
