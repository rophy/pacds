"""Errors that map to a Jev-style HTTP error response."""

from __future__ import annotations

from typing import Any


class PacdsError(Exception):
    """An error with an HTTP status, a machine-readable code and a safe message."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def body(self) -> dict[str, Any]:
        return {"error": {"type": self.code, "message": self.message}}
