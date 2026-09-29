"""Serve recorded model responses for identical requests, so unchanged work is not paid for twice.

A recording is a trace (pacds.engine.trace): each call carries the SHA-256 of its request and the response. A request
whose hash was recorded gets a recorded response instead of a live call; any change upstream of a call (prompt,
questions, tool results, an earlier response) changes its hash, so that call and everything after it runs live.
The same request recorded several times (repeats of one case) gives each recorded response once, then goes live.
A call that failed when recorded with a deterministic error (e.g. 400: the request itself was refused) fails the same
way again, but only once no successful recording of that request is left. Infrastructure failures (connection errors,
429 rate or usage limits, overload, 5xx) and calls cancelled by the time budget say nothing about the request and go
live, so a run that hit a usage limit is replayed up to that point and then completed.
Development only, like traces: recordings hold source code.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from collections import deque
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx
import openai
from openai.types.chat import ChatCompletion

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecordedFailure:
    """A call that failed when recorded: its HTTP status (None for a connection error) and the error."""

    status: int | None
    error: str

    def raise_(self) -> None:
        request = httpx.Request("POST", "http://replay.invalid/v1")
        if self.status is None:
            raise openai.APIConnectionError(message=f"replayed: {self.error}", request=request)
        raise openai.APIStatusError(f"replayed: {self.error}", response=httpx.Response(self.status, request=request), body=None)


class Recordings:
    def __init__(self, traces: Iterable[dict[str, Any]] = ()) -> None:
        self._responses: dict[str, deque[dict[str, Any]]] = {}
        self.recorded = 0
        for trace in traces:
            self.add(trace)

    @classmethod
    def from_dirs(cls, directories: Iterable[Path]) -> Recordings:
        recordings = cls()
        for directory in directories:
            for path in sorted(Path(directory).glob("*.json")):
                try:
                    recordings.add(json.loads(path.read_text()))
                except (OSError, ValueError) as error:
                    logger.warning("skipping recording %s: %s", path.name, error)
        return recordings

    def add(self, trace: dict[str, Any]) -> None:
        for call in trace.get("calls", []):
            if not call.get("request_sha256"):
                continue
            if call.get("response") is None and not _replayable_failure(call):
                continue
            self._responses.setdefault(call["request_sha256"], deque()).append(call)
            self.recorded += 1

    def take(self, request_sha256: str) -> ChatCompletion | RecordedFailure | None:
        recorded = self._responses.get(request_sha256)
        if not recorded:
            return None
        call = next((c for c in recorded if c.get("response") is not None), recorded[0])
        recorded.remove(call)
        if call.get("response") is None:
            last = call["attempts"][-1]
            return RecordedFailure(status=last.get("status"), error=last.get("error") or "")
        return completion(call)


def _replayable_failure(call: dict[str, Any]) -> bool:
    attempts = call.get("attempts") or []
    if not attempts or "CancelledError" in (attempts[-1].get("error") or ""):
        return False
    status = attempts[-1].get("status")
    return status is not None and status < 500 and status not in (408, 409, 429)


def completion(call: dict[str, Any]) -> ChatCompletion:
    """Rebuild the chat-completions response a recorded call received."""
    response, usage = call["response"], call.get("usage") or {}
    tool_calls = [{"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
                  for c in response.get("tool_calls") or []]
    return ChatCompletion.model_validate({
        "id": f"replay-{call.get('n')}", "object": "chat.completion", "created": 0, "model": response.get("model") or "replay",
        "choices": [{"index": 0, "finish_reason": response.get("finish_reason") or "stop",
                     "message": {"role": "assistant", "content": response.get("content"), "tool_calls": tool_calls or None,
                                 **({"anthropic_content": response["anthropic_content"]} if response.get("anthropic_content") else {})}}],
        "usage": {"prompt_tokens": usage.get("input", 0), "completion_tokens": usage.get("output", 0),
                  "total_tokens": usage.get("input", 0) + usage.get("output", 0),
                  "prompt_tokens_details": {"cached_tokens": usage.get("cached", 0)},
                  "completion_tokens_details": {"reasoning_tokens": usage.get("reasoning", 0)}},
    })
