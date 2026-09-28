"""Serve recorded model responses for identical requests, so unchanged work is not paid for twice.

A recording is a trace (pacds.engine.trace): each call carries the SHA-256 of its request and the response. A request
whose hash was recorded gets a recorded response instead of a live call; any change upstream of a call (prompt,
questions, tool results, an earlier response) changes its hash, so that call and everything after it runs live.
The same request recorded several times (repeats of one case) gives each recorded response once, then goes live.
Development only, like traces: recordings hold source code.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from openai.types.chat import ChatCompletion

logger = logging.getLogger(__name__)


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
            if call.get("response") is not None and call.get("request_sha256"):
                self._responses.setdefault(call["request_sha256"], deque()).append(call)
                self.recorded += 1

    def take(self, request_sha256: str) -> ChatCompletion | None:
        recorded = self._responses.get(request_sha256)
        if not recorded:
            return None
        return completion(recorded.popleft())


def completion(call: dict[str, Any]) -> ChatCompletion:
    """Rebuild the chat-completions response a recorded call received."""
    response, usage = call["response"], call.get("usage") or {}
    tool_calls = [{"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
                  for c in response.get("tool_calls") or []]
    return ChatCompletion.model_validate({
        "id": f"replay-{call.get('n')}", "object": "chat.completion", "created": 0, "model": response.get("model") or "replay",
        "choices": [{"index": 0, "finish_reason": response.get("finish_reason") or "stop",
                     "message": {"role": "assistant", "content": response.get("content"), "tool_calls": tool_calls or None}}],
        "usage": {"prompt_tokens": usage.get("input", 0), "completion_tokens": usage.get("output", 0),
                  "total_tokens": usage.get("input", 0) + usage.get("output", 0),
                  "prompt_tokens_details": {"cached_tokens": usage.get("cached", 0)},
                  "completion_tokens_details": {"reasoning_tokens": usage.get("reasoning", 0)}},
    })
