"""One structured audit record per request, written as a JSON line."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, TextIO


@dataclass
class AuditRecord:
    request_id: str
    subject: str | None = None
    git_url: str | None = None
    git_ref: str | None = None
    git_sha: str | None = None
    log_hosts: list[str] = field(default_factory=list)
    questions: dict[str, Any] | None = None
    answers: dict[str, Any] | None = None
    usage: dict[str, int] | None = None
    status: int = 200
    error: str | None = None
    latency_ms: int = 0


class AuditLogger:
    def __init__(self, stream: TextIO = sys.stdout) -> None:
        self._stream = stream

    def write(self, record: AuditRecord) -> None:
        line = {"event": "pacds.audit", "time": datetime.now(UTC).isoformat(), **asdict(record)}
        self._stream.write(json.dumps(line, default=str) + "\n")
        self._stream.flush()
