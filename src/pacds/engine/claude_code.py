"""Run one isolated Claude Code session (`claude -p`) with our tools and a JSON-schema answer.

For llm.api claude_code: a Claude subscription runs the Claude Code CLI but gives no API key. The session sees only the
caller's tools (served in-process over MCP, pacds.engine.mcp_http) and Claude Code's StructuredOutput; built-in tools,
settings, CLAUDE.md, plugins and MCP servers from the environment are all off. The prompt goes in on stdin (an argument
is limited to 128 KiB); the system prompt and the MCP config (its URL carries the server's token) go through files in an
empty private temporary working directory, so neither shows in the process list. The CLI gets a scrubbed environment:
no ANTHROPIC_* (which could switch it to API billing) and nothing of a parent Claude Code session.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import signal
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pacds.engine.mcp_http import SERVER_NAME, Toolset, allowed_tools, serve

EXECUTABLE_ENV = "PACDS_CLAUDE_EXECUTABLE"
STRUCTURED_OUTPUT_TOOL = "StructuredOutput"
_TOOL_PREFIX = f"mcp__{SERVER_NAME}__"
_LIMIT = re.compile(r"usage limit|rate limit|limit reached|too many requests", re.IGNORECASE)
_AUTH = re.compile(r"/login|not logged in|invalid api key|authentication|unauthorized|oauth", re.IGNORECASE)
STREAM_LIMIT_BYTES = 64 * 1024 * 1024


class ClaudeCodeError(Exception):
    """A session that produced no usable result: kind is usage_limit, auth or failed."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class Turn:
    text: str
    tool_calls: list[dict[str, Any]]
    usage: dict[str, int]
    model: str | None


@dataclass
class Result:
    session_id: str | None
    subtype: str
    is_error: bool
    structured_output: Any
    turns: list[Turn]
    tool_results: dict[str, str]
    usage: dict[str, int]
    cost_usd: float | None
    num_turns: int
    text: str
    latency_ms: int
    resumed: bool = False  # a max-turns session and its resume, merged (see merged())
    cwd: str = field(default="", compare=False)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("cwd")
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Result:
        return cls(**{**data, "turns": [Turn(**turn) for turn in data["turns"]]})


def merged(first: Result, resumed: Result) -> Result:
    """A max-turns session and its resume as one outcome: both sessions' turns, tool results and usage, the resume's answer."""
    def total(a: float | None, b: float | None) -> float | None:
        return None if a is None and b is None else (a or 0) + (b or 0)
    return Result(session_id=first.session_id, subtype=resumed.subtype, is_error=resumed.is_error, structured_output=resumed.structured_output,
                  turns=[*first.turns, *resumed.turns], tool_results={**first.tool_results, **resumed.tool_results},
                  usage={key: first.usage.get(key, 0) + resumed.usage.get(key, 0) for key in {**first.usage, **resumed.usage}},
                  cost_usd=total(first.cost_usd, resumed.cost_usd), num_turns=first.num_turns + resumed.num_turns, text=resumed.text,
                  latency_ms=first.latency_ms + resumed.latency_ms, resumed=True)


def _usage(raw: dict[str, Any] | None) -> dict[str, int]:
    raw = raw or {}
    cached, written = raw.get("cache_read_input_tokens") or 0, raw.get("cache_creation_input_tokens") or 0
    return {
        "input": (raw.get("input_tokens") or 0) + cached + written,
        "output": raw.get("output_tokens") or 0,
        "cached": cached,
        "cache_write": written,
        "reasoning": (raw.get("output_tokens_details") or {}).get("thinking_tokens") or 0,
    }


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(block.get("text", "") for block in content or [] if isinstance(block, dict))


def executable() -> list[str]:
    return shlex.split(os.environ.get(EXECUTABLE_ENV) or "claude")


def child_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """The CLI's environment: the parent's without ANTHROPIC_* (API key, base URL: API billing instead of the subscription)
    and without a parent Claude Code session's variables (CLAUDECODE, CLAUDE_EFFORT, CLAUDE_CODE_* but the OAuth token)."""
    source = os.environ if env is None else env
    return {name: value for name, value in source.items()
            if not name.startswith("ANTHROPIC_") and name not in ("CLAUDECODE", "CLAUDE_EFFORT")
            and (not name.startswith("CLAUDE_CODE_") or name == "CLAUDE_CODE_OAUTH_TOKEN")}


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        out.write(text)


def command(*, system_file: str, model: str, schema: dict[str, Any], max_turns: int, effort: str | None = None,
            mcp_config_file: str | None = None, tools: list[str] | tuple[str, ...] = (), resume: str | None = None,
            persist: bool = False, executable: list[str] | None = None) -> list[str]:
    argv = [*(executable or ["claude"]), "-p", "--system-prompt-file", system_file, "--model", model,
            "--tools", "", "--strict-mcp-config", "--setting-sources", "",
            "--json-schema", json.dumps(schema), "--output-format", "stream-json", "--verbose",
            "--max-turns", str(max_turns)]
    if mcp_config_file is not None:
        argv += ["--mcp-config", mcp_config_file]
    if tools:
        argv += ["--allowedTools", ",".join(tools)]
    if effort:
        argv += ["--effort", effort]
    if resume:
        argv += ["--resume", resume]
    if not persist:
        argv.append("--no-session-persistence")
    return argv


class _Stream:
    """Folds stream-json events into a Result."""

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.turns: dict[str, Turn] = {}
        self.tool_results: dict[str, str] = {}
        self.result: dict[str, Any] | None = None
        self._answer_calls: set[str] = set()  # ids of StructuredOutput calls: the answer, not a tool result

    def feed(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        self.session_id = event.get("session_id") or self.session_id
        if kind == "assistant":
            message = event.get("message") or {}
            turn = self.turns.setdefault(message.get("id") or f"turn-{len(self.turns)}", Turn("", [], {}, message.get("model")))
            for block in message.get("content") or []:
                if block.get("type") == "text":
                    turn.text += block.get("text", "")
                elif block.get("type") == "tool_use" and block.get("name") == STRUCTURED_OUTPUT_TOOL:
                    self._answer_calls.add(block.get("id"))
                elif block.get("type") == "tool_use":
                    turn.tool_calls.append({"id": block.get("id"), "name": (block.get("name") or "").removeprefix(_TOOL_PREFIX),
                                            "arguments": json.dumps(block.get("input") or {})})
            if message.get("usage"):
                turn.usage = _usage(message["usage"])  # repeated per content block of one message: the last one wins
        elif kind == "user":
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id") and block["tool_use_id"] not in self._answer_calls:
                    self.tool_results[block["tool_use_id"]] = _text(block.get("content"))
        elif kind == "result":
            self.result = event

    def finish(self, latency_ms: int, cwd: str) -> Result:
        assert self.result is not None
        r = self.result
        return Result(session_id=r.get("session_id") or self.session_id, subtype=r.get("subtype") or "", is_error=bool(r.get("is_error")),
                      structured_output=r.get("structured_output"), turns=list(self.turns.values()), tool_results=self.tool_results,
                      usage=_usage(r.get("usage")), cost_usd=r.get("total_cost_usd"), num_turns=r.get("num_turns") or 0,
                      text=r.get("result") or "", latency_ms=latency_ms, cwd=cwd)


async def run(*, system: str, prompt: str, schema: dict[str, Any], model: str, max_turns: int, toolset: Toolset | None = None,
              effort: str | None = None, resume: str | None = None, persist: bool = False, cwd: str | None = None,
              timeout: float | None = None) -> Result:
    """One session. `cwd` reuses a working directory (a resume must run where its session was created). `timeout` (seconds)
    bounds the whole session: past it the CLI is killed and ClaudeCodeError("failed") raised."""
    try:
        async with asyncio.timeout(timeout) as scope:
            return await _run(system=system, prompt=prompt, schema=schema, model=model, max_turns=max_turns, toolset=toolset,
                              effort=effort, resume=resume, persist=persist, cwd=cwd)
    except TimeoutError:
        if scope.expired():
            raise ClaudeCodeError("failed", f"claude session timed out after {timeout:g} s") from None
        raise


async def _run(*, system: str, prompt: str, schema: dict[str, Any], model: str, max_turns: int, toolset: Toolset | None,
               effort: str | None, resume: str | None, persist: bool, cwd: str | None) -> Result:
    started = time.monotonic()
    owned = cwd is None
    workdir = cwd or tempfile.mkdtemp(prefix="pacds-claude-")
    failed = True
    try:
        system_file = Path(workdir) / "system-prompt.txt"
        _write_private(system_file, system)
        if toolset is None:
            result = await _session(command(system_file=str(system_file), model=model, schema=schema, max_turns=max_turns, effort=effort,
                                            resume=resume, persist=persist, executable=executable()), prompt, workdir, started)
            failed = False
            return result
        async with serve(toolset) as mcp_config:
            mcp_file = Path(workdir) / "mcp-config.json"
            _write_private(mcp_file, json.dumps(mcp_config))
            argv = command(system_file=str(system_file), model=model, schema=schema, max_turns=max_turns, effort=effort,
                           mcp_config_file=str(mcp_file), tools=allowed_tools(toolset), resume=resume, persist=persist,
                           executable=executable())
            result = await _session(argv, prompt, workdir, started)
            failed = False
            return result
    finally:
        if owned and failed and persist:  # a failed run returns no cwd, so nobody could forget its session
            _forget(workdir)
        elif owned and (failed or not persist):
            _remove(workdir)


async def _session(argv: list[str], prompt: str, cwd: str, started: float) -> Result:
    process = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True, limit=STREAM_LIMIT_BYTES, env=child_env())
    stream = _Stream()
    stderr_task: asyncio.Task[bytes] | None = None
    try:
        assert process.stdin and process.stdout and process.stderr
        stderr_task = asyncio.create_task(process.stderr.read())
        process.stdin.write(prompt.encode())
        await process.stdin.drain()
        process.stdin.close()
        async for line in process.stdout:
            if line.strip():
                try:
                    stream.feed(json.loads(line))
                except json.JSONDecodeError:
                    continue
        await process.wait()
        stderr = (await stderr_task).decode(errors="replace")[-2000:]
    except BaseException as error:  # cancelled by a time budget, or anything else: never leave the CLI running
        _kill(process)
        if stderr_task is not None:
            stderr_task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(_reap(process)), 10)
        except BaseException:  # noqa: BLE001 - the original error matters more
            pass
        if isinstance(error, (BrokenPipeError, ConnectionResetError, ValueError)):  # ValueError: a line over the stream limit
            raise ClaudeCodeError("failed", f"claude session I/O failed: {error!r}") from error
        raise
    if stream.result is None:
        kind = "auth" if _AUTH.search(stderr) else "failed"
        raise ClaudeCodeError(kind, f"claude exited {process.returncode} without a result: {stderr.strip()[-500:]}")
    result = stream.finish(round((time.monotonic() - started) * 1000), cwd)
    if result.is_error and result.subtype != "error_max_turns":
        text = f"{result.text} {stderr}"
        if stream.result.get("api_error_status") == 429 or _LIMIT.search(text):
            raise ClaudeCodeError("usage_limit", f"Claude usage or rate limit: {result.text[:300]}")
        raise ClaudeCodeError("auth" if _AUTH.search(text) else "failed", f"claude session failed ({result.subtype}): {result.text[:300]}")
    return result


async def _reap(process: asyncio.subprocess.Process) -> None:
    """Drain stdout to EOF (a reader that stopped on an oversized line leaves the pipe paused, so the exit is never seen) and reap the child."""
    assert process.stdout
    while await process.stdout.read(65536):
        pass
    await process.wait()


def _kill(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _remove(path: str) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def transcript_dir(cwd: str) -> Path:
    """Where the CLI keeps a persisted session's transcript: ~/.claude/projects/<cwd with - for non-alphanumerics>."""
    return Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _forget(cwd: str) -> None:
    path = Path(cwd).resolve()
    if not path.name.startswith("pacds-claude-") or path.parent != Path(tempfile.gettempdir()).resolve():
        return  # only ever delete a directory `run` created
    _remove(str(path))
    _remove(str(transcript_dir(cwd)))


def forget_session(result: Result) -> None:
    """Delete a persisted session's working directory and transcript."""
    if result.cwd:
        _forget(result.cwd)


async def ask_json(*, system: str, prompt: str, schema: dict[str, Any], model: str, effort: str | None = None,
                   timeout: float | None = None) -> tuple[dict[str, Any], dict[str, int]]:
    """One answer without tools (baseline-style clients: classifier, casebook reviews)."""
    result = await run(system=system, prompt=prompt, schema=schema, model=model, max_turns=2, effort=effort, timeout=timeout)
    if not isinstance(result.structured_output, dict):
        raise ClaudeCodeError("failed", f"no structured output ({result.subtype})")
    return result.structured_output, result.usage
