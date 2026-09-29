import asyncio
import json
import os
import re
import tempfile
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
                   mcp_config_file="/tmp/mcp.json", tools=["mcp__pacds__read_file"])
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
    log = fake_claude("hang")
    task = asyncio.create_task(claude_code.run(system="s", prompt="p", schema=SCHEMA, model="haiku", max_turns=1))
    await asyncio.sleep(1.0)
    pid = _calls(log)[0]["pid"]
    os.kill(pid, 0)  # alive before the cancel
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(40):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


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


def test_forget_session_only_deletes_what_run_created(tmp_path):
    other = Path(tempfile.mkdtemp(prefix="keep-me-"))
    ours = Path(tempfile.mkdtemp(prefix="pacds-claude-"))
    try:
        for cwd in (other, tmp_path):  # wrong name, and right-ish parent / wrong parent
            claude_code.forget_session(claude_code.Result(session_id="s", subtype="success", is_error=False, structured_output=None,
                                                          turns=[], tool_results={}, usage={}, cost_usd=None, num_turns=1, text="",
                                                          latency_ms=1, cwd=str(cwd)))
            assert cwd.exists()
        claude_code.forget_session(claude_code.Result(session_id="s", subtype="success", is_error=False, structured_output=None,
                                                      turns=[], tool_results={}, usage={}, cost_usd=None, num_turns=1, text="",
                                                      latency_ms=1, cwd=str(ours)))
        assert not ours.exists()
    finally:
        for d in (other, ours):
            if d.exists():
                d.rmdir()


async def test_oversized_stream_line_is_a_failed_error(fake_claude, monkeypatch):
    fake_claude("success.jsonl")
    monkeypatch.setattr(claude_code, "STREAM_LIMIT_BYTES", 100)
    with pytest.raises(ClaudeCodeError) as error:
        await claude_code.run(system="s", prompt="p", schema=SCHEMA, model="haiku", max_turns=1)
    assert error.value.kind == "failed"


async def test_broken_stdin_is_a_failed_error(fake_claude):
    fake_claude("noread")
    with pytest.raises(ClaudeCodeError) as error:
        await claude_code.run(system="s", prompt="x" * 5_000_000, schema=SCHEMA, model="haiku", max_turns=1)
    assert error.value.kind == "failed"


async def test_mcp_config_goes_in_a_private_file_not_argv(fake_claude):
    log = fake_claude("success.jsonl", tool='read_file:{"path": "retry.py"}')

    async def call(name: str, arguments: dict) -> str:
        return "served text"
    result = await claude_code.run(system="s", prompt="p", schema=SCHEMA, model="haiku", max_turns=5, toolset=Toolset(DEFS, call))
    assert result.tool_results["t1"] == "served text"  # the fake reached the tool through the file's URL
    call_ = _calls(log)[0]
    argv = call_["argv"]
    config = Path(argv[argv.index("--mcp-config") + 1])
    assert config.parent == Path(call_["cwd"]) and call_["mcp_mode"] == "0o600"
    assert not any("/mcp/" in arg or "127.0.0.1" in arg for arg in argv)


async def test_cli_env_drops_api_and_parent_session_variables(fake_claude, monkeypatch):
    log = fake_claude("success.jsonl")
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "CLAUDECODE", "CLAUDE_EFFORT",
                 "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_ENTRYPOINT"):
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "x")
    monkeypatch.setenv("DISABLE_AUTOUPDATER", "1")
    await claude_code.run(system="s", prompt="p", schema=SCHEMA, model="haiku", max_turns=5)
    env = set(_calls(log)[0]["env"])
    assert not {name for name in env if name.startswith("ANTHROPIC_")}
    assert not {name for name in env if name.startswith("CLAUDE_CODE_") and name != "CLAUDE_CODE_OAUTH_TOKEN"}
    assert "CLAUDECODE" not in env and "CLAUDE_EFFORT" not in env
    assert {"CLAUDE_CODE_OAUTH_TOKEN", "DISABLE_AUTOUPDATER", "PATH", "HOME", "FAKE_CLAUDE_LOG", "FAKE_CLAUDE_STREAM"} <= env


def _transcripts(home: Path, cwd: str) -> Path:
    return home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd)


async def test_failed_persisted_session_leaves_no_transcript(fake_claude, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    log = fake_claude("usage_limit.jsonl")
    with pytest.raises(ClaudeCodeError):
        await claude_code.run(system="s", prompt="p", schema=SCHEMA, model="haiku", max_turns=1, persist=True)
    cwd = _calls(log)[0]["cwd"]
    assert not Path(cwd).exists()
    assert (tmp_path / "home" / ".claude" / "projects").is_dir()  # the fake did write one
    assert not _transcripts(tmp_path / "home", cwd).exists()


async def test_forget_session_removes_the_transcript(fake_claude, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    fake_claude("success.jsonl")
    result = await claude_code.run(system="s", prompt="p", schema=SCHEMA, model="haiku", max_turns=1, persist=True)
    assert _transcripts(tmp_path / "home", result.cwd).is_dir()
    claude_code.forget_session(result)
    assert not Path(result.cwd).exists() and not _transcripts(tmp_path / "home", result.cwd).exists()


async def test_timeout_is_a_failed_error_and_kills_the_cli(fake_claude):
    log = fake_claude("hang")
    with pytest.raises(ClaudeCodeError) as error:
        await claude_code.run(system="s", prompt="p", schema=SCHEMA, model="haiku", max_turns=1, timeout=1.0)
    assert error.value.kind == "failed" and "timed out" in str(error.value)
    pid = _calls(log)[0]["pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_ask_json_timeout(fake_claude):
    fake_claude("hang")
    with pytest.raises(ClaudeCodeError) as error:
        await claude_code.ask_json(system="s", prompt="p", schema=SCHEMA, model="haiku", timeout=1.0)
    assert "timed out" in str(error.value)
