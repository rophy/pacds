"""What the final-request tools and a context budget do to recorded investigations: offline, from PACDS traces.

For each live investigation the prompt of every model call is rebuilt from the trace, sized with a tokens-per-character
ratio fitted to that investigation's recorded usage, and replayed under a policy: the engine's context budget
(llm.context_budget_tokens: the oldest large tool results replaced by a note down to two thirds of the budget) and the
final request with or without the tools. Reported per policy: input tokens, uncached input tokens (a prefix cache
matching whole messages against the previous request, as vLLM's and hosted providers' do), the largest prompt, and how
many investigations the budget touched. The model's trajectory is held fixed, so this measures cost and context, not
accuracy: a removed result the model needed shows up only in a live or replayed run.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from typing import Any

from pacds.engine.agent_provider import ELIDED_RESULT, KEEP_RECENT_RESULTS, MIN_ELIDED_CHARS
from pacds.engine.trace import messages_at

NOTE_CHARS = len(ELIDED_RESULT.format(chars=10000))


@dataclass(frozen=True)
class Investigation:
    messages: list[dict[str, Any]]  # the transcript the final request carried
    requests: list[int]  # message count of each investigation request
    ratio: float  # tokens per character of JSON-encoded messages
    overhead: float  # tokens outside the messages (tool definitions, formatting)


@dataclass(frozen=True)
class Outcome:
    input: float
    uncached: float
    peak: float
    compacted: bool


def _size(message: dict[str, Any]) -> int:
    return len(json.dumps(message))


def investigation(trace: dict[str, Any]) -> Investigation | None:
    """The investigation of a live, answered request; None when it was replayed, failed or has no usage."""
    calls = trace.get("calls") or []
    if trace.get("status") != 200 or not calls or any(call.get("replayed") or not call.get("usage") for call in calls):
        return None
    investigate = [n for n, call in enumerate(calls, 1) if call["phase"] == "investigate"]
    final = next((n for n, call in enumerate(calls, 1) if call["phase"] == "final"), None)
    if not investigate or final is None:
        return None
    requests = [len(messages_at(calls, n)) for n in investigate]
    first, last = (sum(_size(m) for m in messages_at(calls, n)) for n in (investigate[0], investigate[-1]))
    tokens_first, tokens_last = (calls[n - 1]["usage"]["input"] for n in (investigate[0], investigate[-1]))
    ratio = (tokens_last - tokens_first) / (last - first) if last > first else tokens_first / first
    return Investigation(messages=messages_at(calls, final)[:-1], requests=requests, ratio=ratio, overhead=tokens_first - first * ratio)


def simulate(inv: Investigation, *, budget: int | None, final_keeps_tools: bool) -> Outcome:
    sizes = [_size(m) for m in inv.messages]
    elided: set[int] = set()

    def tokens(count: int) -> float:
        return sum(NOTE_CHARS if i in elided else sizes[i] for i in range(count)) * inv.ratio + inv.overhead

    total = uncached = peak = 0.0
    previous: list[tuple[int, bool]] = []
    for count in inv.requests:
        if budget and tokens(count) > budget:
            results = [i for i in range(count) if inv.messages[i]["role"] == "tool" and i not in elided]
            for i in results[:-KEEP_RECENT_RESULTS]:
                if tokens(count) <= budget * 2 // 3:
                    break
                if len(inv.messages[i].get("content") or "") >= MIN_ELIDED_CHARS:
                    elided.add(i)
        current = [(NOTE_CHARS if i in elided else sizes[i], i in elided) for i in range(count)]
        shared = 0
        while shared < min(len(previous), count) and previous[shared] == current[shared]:
            shared += 1
        prompt = tokens(count)
        cached = sum(size for size, _ in current[:shared]) * inv.ratio + inv.overhead if shared else 0.0
        total, uncached, peak = total + prompt, uncached + prompt - cached, max(peak, prompt)
        previous = current
    final = tokens(len(inv.messages))
    # Without the tools the final prompt differs from the start (tools are rendered before the messages).
    cached = sum(size for size, _ in previous) * inv.ratio + inv.overhead if final_keeps_tools else 0.0
    return Outcome(input=total + final, uncached=uncached + final - cached, peak=max(peak, final), compacted=bool(elided))


def policies(budgets: list[int]) -> list[tuple[str, int | None, bool]]:
    rows: list[tuple[str, int | None, bool]] = [("final without tools, no budget (before)", None, False), ("final with tools, no budget", None, True)]
    return rows + [(f"final with tools, budget {budget:,}", budget, True) for budget in budgets]


def table(traces: list[dict[str, Any]], budgets: list[int]) -> str:
    investigations = [inv for inv in map(investigation, traces) if inv is not None]
    if not investigations:
        return "no live investigations with usage in these traces"
    lines = [f"{len(investigations)} live investigations of {len(traces)} PACDS traces", "",
             "| policy | input | uncached | largest prompt p50 / p90 / max | budget applied |", "|---|---|---|---|---|"]
    for label, budget, keeps_tools in policies(budgets):
        outcomes = [simulate(inv, budget=budget, final_keeps_tools=keeps_tools) for inv in investigations]
        peaks = sorted(outcome.peak for outcome in outcomes)
        lines.append(f"| {label} | {sum(o.input for o in outcomes) / 1e6:.1f}M | {sum(o.uncached for o in outcomes) / 1e6:.1f}M | "
                     f"{statistics.median(peaks) / 1e3:.0f}K / {peaks[int(len(peaks) * 0.9)] / 1e3:.0f}K / {peaks[-1] / 1e3:.0f}K | "
                     f"{sum(o.compacted for o in outcomes)} |")
    return "\n".join(lines)
