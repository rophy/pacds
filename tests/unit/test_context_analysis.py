"""The offline context-policy simulation (pacds_eval/analysis/context.py)."""

from pacds_eval.analysis.context import investigation, simulate, table


def trace(results: int = 8, result_chars: int = 20000) -> dict:
    """An investigation of `results` tool calls, each result `result_chars` long; usage grows with the transcript."""
    calls, messages = [], [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
    kept = 0
    for n in range(results + 1):
        added = messages[kept:]
        calls.append({"phase": "investigate", "kept": kept, "messages_added": added, "usage": {"input": 100 + n * result_chars // 4}})
        kept = len(messages)
        if n < results:
            messages += [{"role": "assistant", "content": "", "tool_calls": [{"id": f"c{n}", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
                         {"role": "tool", "tool_call_id": f"c{n}", "content": "x" * result_chars}]
    calls.append({"phase": "final", "kept": kept, "messages_added": messages[kept:] + [{"role": "user", "content": "answer"}], "usage": {"input": 1}})
    return {"status": 200, "calls": calls}


def test_final_with_tools_is_cached_and_a_budget_caps_the_prompt():
    inv = investigation(trace())
    assert inv is not None and len(inv.requests) == 9
    before = simulate(inv, budget=None, final_keeps_tools=False)
    after = simulate(inv, budget=None, final_keeps_tools=True)
    assert after.input == before.input and after.uncached < before.uncached - 0.8 * before.peak
    capped = simulate(inv, budget=24000, final_keeps_tools=True)
    assert capped.compacted and capped.input < after.input and before.peak > 40000
    # The latest KEEP_RECENT_RESULTS results always stay, so the cap holds only down to them
    assert capped.peak < before.peak


def test_replayed_or_failed_investigations_are_skipped():
    replayed = trace()
    replayed["calls"][0]["replayed"] = True
    assert investigation(replayed) is None and investigation({**trace(), "status": 504}) is None
    assert "1 live investigations of 3 PACDS traces" in table([trace(), replayed, {**trace(), "status": 504}], [32000])


def test_claude_code_sessions_have_no_investigation():
    calls = [{"phase": "claude_code", "kept": 0, "messages_added": [], "usage": {"input": 5}, "request_sha256": None}]
    assert investigation({"status": 200, "calls": calls}) is None
