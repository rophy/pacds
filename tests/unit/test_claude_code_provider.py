import asyncio
import json
from pathlib import Path

import pytest
from system_one_adapter.providers.base import Message
from typesafe_sdk import TypeSafeAPIError, TypeSafeError

from pacds.engine.agent_provider import AgentBudgetExceeded
from pacds.engine.claude_code_provider import ClaudeCodeProvider
from pacds.engine.replay import Recordings
from pacds.engine.trace import Trace

SCHEMA = {"type": "object", "properties": {"answers": {"type": "object", "properties": {"q": {"type": "string"}}}},
          "required": ["answers"], "additionalProperties": False}
MESSAGES = [Message(role="system", content="adapter system"), Message(role="user", content="<document>{}</document>")]


class Tools:
    definitions = [{"type": "function", "function": {"name": "read_file", "description": "Read.",
                                                     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]

    def __init__(self):
        self.calls = []

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        return "file text"

    async def fingerprint(self):
        return {"commit": "abc", "logs": {}}


def _read(log):
    return [json.loads(line) for line in log.read_text().splitlines()]


def _provider(tools=None, trace=None, replay=None, budget=30.0):
    return ClaudeCodeProvider(model_name="haiku", tools=tools if tools is not None else Tools(), max_turns=4,
                              time_budget_seconds=budget, trace=trace, replay=replay)


async def test_investigates_and_answers(fake_claude):
    log = fake_claude("success.jsonl", tool='read_file:{"path": "retry.py"}')
    calls = lambda: _read(log)
    tools, trace = Tools(), Trace()
    result = await _provider(tools, trace).request(MESSAGES, schema=SCHEMA, structured=True)
    assert json.loads(result.text) == {"answers": {"q": "yes"}}
    assert result.input_tokens > 0 and result.output_tokens == 50
    assert tools.calls == [("read_file", {"path": "retry.py"})]
    assert trace.tools[0]["name"] == "read_file"
    assert trace.info["sessions"][0]["label"] == "investigate"
    argv = calls()[0]["argv"]
    assert argv[argv.index("--max-turns") + 1] == "5"
    assert "--no-session-persistence" not in argv  # resumable
    assert "<document>{}</document>" in calls()[0]["stdin"] and "adapter system" not in calls()[0]["stdin"]


async def test_max_turns_resumes_once_with_the_final_instruction(fake_claude):
    log = fake_claude("max_turns.jsonl,success.jsonl")
    calls = lambda: _read(log)
    trace = Trace()
    result = await _provider(trace=trace).request(MESSAGES, schema=SCHEMA, structured=True)
    assert json.loads(result.text) == {"answers": {"q": "yes"}}
    first, second = calls()
    assert second["argv"][second["argv"].index("--resume") + 1] == "s-2"
    assert second["argv"][second["argv"].index("--max-turns") + 1] == "2"
    assert second["cwd"] == first["cwd"]
    assert "investigation is over" in second["stdin"]
    assert [s["label"] for s in trace.info["sessions"]] == ["investigate", "resume"]
    assert not Path(first["cwd"]).exists()  # forgotten


async def test_usage_limit_is_overloaded(fake_claude):
    fake_claude("usage_limit.jsonl")
    provider = _provider()
    with pytest.raises(TypeSafeAPIError) as error:
        await provider.request(MESSAGES, schema=SCHEMA, structured=True)
    assert error.value.status == 529


async def test_auth_failure_is_an_engine_error(fake_claude):
    fake_claude("auth.jsonl")
    with pytest.raises(TypeSafeError):
        await _provider().request(MESSAGES, schema=SCHEMA, structured=True)


async def test_time_budget(fake_claude):
    fake_claude("hang")
    trace = Trace()
    with pytest.raises(AgentBudgetExceeded):
        await _provider(trace=trace, budget=1.0).request(MESSAGES, schema=SCHEMA, structured=True)
    assert trace.info["investigation"]["reason"] == "time_budget"


async def test_replay_serves_a_recorded_session(fake_claude):
    log = fake_claude("success.jsonl")
    calls = lambda: _read(log)
    recorded = Trace()
    await _provider(trace=recorded).request(MESSAGES, schema=SCHEMA, structured=True)
    live_calls = len(calls())
    trace = Trace()
    result = await _provider(trace=trace, replay=Recordings([recorded.to_dict()])).request(MESSAGES, schema=SCHEMA, structured=True)
    assert json.loads(result.text) == {"answers": {"q": "yes"}}
    assert len(calls()) == live_calls  # no new CLI run
    assert trace.info["sessions"][0]["replayed"] is True


async def test_correction_is_a_fresh_session(fake_claude):
    log = fake_claude("success.jsonl")
    calls = lambda: _read(log)
    provider = _provider()
    first = await provider.request(MESSAGES, schema=SCHEMA, structured=True)
    corrected = [*MESSAGES, Message(role="assistant", content=first.text), Message(role="user", content="fix the answer")]
    await provider.request(corrected, schema=SCHEMA, structured=True)
    second = calls()[-1]
    assert "--resume" not in second["argv"] and "--mcp-config" not in second["argv"]
    assert "Your previous answer" in second["stdin"] and "fix the answer" in second["stdin"]


async def test_max_turns_investigation_is_recorded_and_replayed_whole(fake_claude):
    log = fake_claude("max_turns.jsonl,success.jsonl", tool='read_file:{"path": "retry.py"}')
    recorded = Trace()
    await _provider(trace=recorded).request(MESSAGES, schema=SCHEMA, structured=True)
    investigate, resume = recorded.info["sessions"]
    assert investigate["label"] == "investigate" and investigate["request_sha256"]
    assert investigate["result"]["structured_output"] == {"answers": {"q": "yes"}}  # the resumed outcome, under the first hash
    assert len(investigate["result"]["turns"]) == 3 and investigate["result"]["num_turns"] == 5
    assert resume["label"] == "resume" and resume["request_sha256"] is None
    live_calls = len(_read(log))
    trace = Trace()
    result = await _provider(trace=trace, replay=Recordings([recorded.to_dict()])).request(MESSAGES, schema=SCHEMA, structured=True)
    assert json.loads(result.text) == {"answers": {"q": "yes"}}
    assert len(_read(log)) == live_calls  # no CLI run
    assert [s["label"] for s in trace.info["sessions"]] == ["investigate"] and trace.info["sessions"][0]["replayed"] is True
    assert len(trace.calls) == 3 and all(call["replayed"] for call in trace.calls)
    assert trace.info["investigation"]["reason"] == "turn_budget_resumed"
    assert [(t["call"], t["name"]) for t in trace.tools] == [(t["call"], t["name"]) for t in recorded.tools] == [(1, "read_file"), (2, "read_file")]


async def test_live_tool_records_point_at_the_turn_that_called_them(fake_claude):
    fake_claude("success.jsonl", tool='read_file:{"path": "retry.py"}')
    live = Trace()
    await _provider(trace=live).request(MESSAGES, schema=SCHEMA, structured=True)
    replayed = Trace()
    await _provider(trace=replayed, replay=Recordings([live.to_dict()])).request(MESSAGES, schema=SCHEMA, structured=True)
    assert live.calls[0]["response"]["tool_calls"][0]["name"] == "read_file"
    assert [t["call"] for t in live.tools] == [t["call"] for t in replayed.tools] == [1]


async def test_correction_session_is_time_limited(fake_claude):
    fake_claude("success.jsonl,hang")
    provider = _provider(budget=1.0)
    first = await provider.request(MESSAGES, schema=SCHEMA, structured=True)
    corrected = [*MESSAGES, Message(role="assistant", content=first.text), Message(role="user", content="fix the answer")]
    with pytest.raises(TypeSafeError) as error:
        async with asyncio.timeout(15):  # without the limit the fake hangs for an hour
            await provider.request(corrected, schema=SCHEMA, structured=True)
    assert "timed out" in str(error.value)


async def test_a_request_after_a_failed_one_investigates_again(fake_claude):
    log = fake_claude("usage_limit.jsonl,success.jsonl")
    provider = _provider()
    with pytest.raises(TypeSafeAPIError):
        await provider.request(MESSAGES, schema=SCHEMA, structured=True)
    result = await provider.request(MESSAGES, schema=SCHEMA, structured=True)
    assert json.loads(result.text) == {"answers": {"q": "yes"}}
    second = _read(log)[1]
    assert "--mcp-config" in second["argv"] and "Your previous answer" not in second["stdin"]
