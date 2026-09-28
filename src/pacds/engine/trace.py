"""Per-request record of every model call and tool call, for offline evaluation analysis.

Messages are stored as deltas: call n's messages are call n-1's first `kept` messages plus `messages_added`, so a
trace grows linearly with the investigation instead of quadratically. Traces hold source code and log content:
PACDS writes them only in development configurations (see TraceConfig), never into a response.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any


def sha256(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


class CallRecord:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    def attempt(self, started: float, *, status: int | None = 200, error: str | None = None) -> None:
        entry: dict[str, Any] = {"status": status, "latency_ms": _elapsed_ms(started)}
        if error:
            entry["error"] = error[:300]
        self.data["attempts"].append(entry)

    def replayed(self) -> None:
        """The response came from a recording (pacds.engine.replay), not from the model."""
        self.data["replayed"] = True

    def respond(self, response: Any, started: float) -> None:
        """Record a chat-completions response (the Responses API is translated to one first)."""
        choice = response.choices[0]
        calls = choice.message.tool_calls or []
        self.data["response"] = {
            "content": choice.message.content,
            "tool_calls": [{"id": c.id, "name": c.function.name, "arguments": c.function.arguments} for c in calls],
            "finish_reason": choice.finish_reason,
            "model": response.model,
        }
        self.data["usage"] = usage_of(response)
        self.data["latency_ms"] = _elapsed_ms(started)


def usage_of(response: Any) -> dict[str, int]:
    usage = response.usage
    if usage is None:
        return {"input": 0, "output": 0, "cached": 0, "reasoning": 0}
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    completion_details = getattr(usage, "completion_tokens_details", None)
    return {
        "input": usage.prompt_tokens or 0,
        "output": usage.completion_tokens or 0,
        "cached": (getattr(prompt_details, "cached_tokens", None) or 0),
        "reasoning": (getattr(completion_details, "reasoning_tokens", None) or 0),
    }


class Trace:
    def __init__(self, **info: Any) -> None:
        # Request-level fields (request id, inputs, config, outcome); the caller fills them in as they become known.
        self.info: dict[str, Any] = dict(info)
        self.calls: list[dict[str, Any]] = []
        self.tools: list[dict[str, Any]] = []
        self._sent: list[Any] = []

    def start_call(self, phase: str, messages: list[dict[str, Any]], request: dict[str, Any]) -> CallRecord:
        """Record a model call about to be made; `request` is everything sent besides the model, for its hash."""
        kept = 0
        for previous, current in zip(self._sent, messages):
            if previous is not current and previous != current:
                break
            kept += 1
        record = CallRecord({
            "n": len(self.calls) + 1,
            "phase": phase,
            "kept": kept,
            "messages_added": json.loads(json.dumps(messages[kept:], default=str)),
            "request_sha256": sha256(request),
            "response": None,
            "usage": None,
            "latency_ms": None,
            "attempts": [],
        })
        self._sent = list(messages)
        self.calls.append(record.data)
        return record

    def add_tool(self, name: str, arguments: Any, result: str, started: float, **extra: Any) -> None:
        self.tools.append({
            "call": len(self.calls),
            "name": name,
            "arguments": arguments,
            "result": result,
            "result_chars": len(result),
            "latency_ms": _elapsed_ms(started),
            "error": result.startswith("error:"),
            **extra,
        })

    def usage(self) -> dict[str, int]:
        """Totals over every call; replayed calls are counted too (as recorded) and also reported apart."""
        total = {"input": 0, "output": 0, "cached": 0, "reasoning": 0, "calls": len(self.calls), "replayed_calls": 0, "live_input": 0}
        for call in self.calls:
            for key, value in (call["usage"] or {}).items():
                total[key] += value
            if call.get("replayed"):
                total["replayed_calls"] += 1
            else:
                total["live_input"] += (call["usage"] or {}).get("input", 0)
        return total

    def to_dict(self) -> dict[str, Any]:
        return {**self.info, "calls": self.calls, "tools": self.tools, "usage": self.usage()}


def messages_at(calls: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    """Rebuild the full message list sent with call n (1-based) from the deltas."""
    messages: list[dict[str, Any]] = []
    for call in calls[:n]:
        messages = messages[: call["kept"]] + call["messages_added"]
    return messages
