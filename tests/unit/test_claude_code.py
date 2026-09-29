import asyncio
import json
from pathlib import Path

import pytest

from pacds.engine import claude_code
from pacds.engine.claude_code import ClaudeCodeError, command
from pacds.engine.mcp_http import Toolset

SCHEMA = {"type": "object", "properties": {"answers": {"type": "object"}}, "required": ["answers"]}
DEFS = [{"type": "function", "function": {"name": "read_file", "description": "Read.",
                                          "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}]


def _calls(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_command_isolates_the_session():
    argv = command(system_file="/tmp/s.txt", model="haiku", schema=SCHEMA, max_turns=5,
                   mcp_config={"mcpServers": {}}, tools=["mcp__pacds__read_file"])
    joined = " ".join(argv)
    for flag in ("-p", "--tools", "--strict-mcp-config", "--setting-sources", "--no-session-persistence",
                 "--system-prompt-file", "--json-schema", "--output-format", "--verbose"):
        assert flag in argv, flag
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--allowedTools") + 1] == "mcp__pacds__read_file"
    assert argv[argv.index("--max-turns") + 1] == "5"
    assert "--bare" not in argv and "--resume" not in joined


def test_command_persists_and_resumes_when_asked():
    argv = command(system_file="/s", model="haiku", schema=SCHEMA, max_turns=2, resume="s-2", persist=True, effort="low")
    assert "--no-session-persistence" not in argv
    assert argv[argv.index("--resume") + 1] == "s-2"
    assert argv[argv.index("--effort") + 1] == "low"
    assert "--mcp-config" not in argv  # no tools: no MCP server


async def test_success_parses_turns_tools_and_usage(fake_claude):
    log = fake_claude("success.jsonl")
    result = await claude_code.run(system="sys", prompt="the prompt", schema=SCHEMA, model="haiku", max_turns=5)
    assert result.subtype == "success" and result.structured_output == {"answers": {"q": "yes"}}
    assert result.session_id == "s-1" and result.num_turns == 3 and result.cost_usd == 0.004
    assert [t.text for t in result.turns] == ["Reading.", ""]
    assert result.turns[0].tool_calls == [{"id": "t1", "name": "read_file", "arguments": '{"path": "retry.py"}'}]
    assert result.turns[1].tool_calls == []  # StructuredOutput is the answer, not a tool call
    assert result.turns[1].usage == {"input": 952, "output": 30, "cached": 900, "cache_write": 50, "reasoning": 7}
    assert result.tool_results == {"t1": "def retry(): pass"}
    assert result.usage["input"] == 7 + 900 + 950 and result.usage["output"] == 50
    call = _calls(log)[0]
    assert call["stdin"] == "the prompt"
    assert Path(call["cwd"]).name.startswith("pacds-claude-") and not Path(call["cwd"]).exists()  # removed after
    assert Path(call["argv"][call["argv"].index("--system-prompt-file") + 1]).parent == Path(call["cwd"])


async def test_max_turns_is_returned_for_the_caller(fake_claude):
    fake_claude("max_turns.jsonl")
    result = await claude_code.run(system="s", prompt="p", schema=SCHEMA, model="haiku", max_turns=1)
    assert result.subtype == "error_max_turns" and result.structured_output is None
    assert result.tool_results == {"t1": "x = 1"}  # list-of-blocks content flattened


async def test_usage_limit_and_auth_raise(fake_claude):
    fake_claude("usage_limit.jsonl")
    with pytest.raises(ClaudeCodeError) as limit:
        await claude_code.run(system="s", prompt="p", schema=SCHEMA, model="haiku", max_turns=1)
    assert limit.value.kind == "usage_limit"
    fake_claude("auth.jsonl")
    with pytest.raises(ClaudeCodeError) as auth:
        await claude_code.run(system="s", prompt="p", schema=SCHEMA, model="haiku", max_turns=1)
    assert auth.value.kind == "auth" and "login" in str(auth.value)


async def test_tools_are_served_over_mcp(fake_claude):
    calls = []

    async def call(name: str, arguments: dict) -> str:
        calls.append((name, arguments))
        return "served text"
    fake_claude("success.jsonl", tool='read_file:{"path": "retry.py"}')
    result = await claude_code.run(system="s", prompt="p", schema=SCHEMA, model="haiku", max_turns=5, toolset=Toolset(DEFS, call))
    assert calls == [("read_file", {"path": "retry.py"})]
    assert result.tool_results["t1"] == "served text"


async def test_cancel_kills_the_process(fake_claude):
    fake_claude("hang")
    task = asyncio.create_task(claude_code.run(system="s", prompt="p", schema=SCHEMA, model="haiku", max_turns=1))
    await asyncio.sleep(1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_ask_json(fake_claude):
    fake_claude("success.jsonl")
    answer, usage = await claude_code.ask_json(system="s", prompt="p", schema=SCHEMA, model="haiku")
    assert answer == {"answers": {"q": "yes"}} and usage["output"] == 50


def test_result_round_trips():
    result = claude_code.Result(session_id="s", subtype="success", is_error=False, structured_output={"a": 1},
                                turns=[claude_code.Turn(text="t", tool_calls=[], usage={"input": 1}, model="m")],
                                tool_results={}, usage={"input": 1}, cost_usd=0.1, num_turns=1, text="", latency_ms=5)
    assert claude_code.Result.from_dict(json.loads(json.dumps(result.to_dict()))) == result


async def test_owned_workdir_is_removed_when_the_session_fails_even_if_persisted(fake_claude):
    log = fake_claude("auth.jsonl")
    with pytest.raises(ClaudeCodeError):
        await claude_code.run(system="s", prompt="p", schema=SCHEMA, model="haiku", max_turns=1, persist=True)
    assert not Path(_calls(log)[0]["cwd"]).exists()
