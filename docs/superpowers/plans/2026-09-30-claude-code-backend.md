# Claude Code Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `llm.api: claude_code` / `LLM_API=claude_code` runs PACDS investigations and every eval client through the
Claude Code CLI (`claude -p`) on a Claude subscription, with no API key.

**Architecture:** A runner (`pacds.engine.claude_code`) starts one isolated `claude -p` session per investigation or
client call, serves the caller's tools to it from an in-process MCP server on localhost (`pacds.engine.mcp_http`), and
parses the `stream-json` events into a `Result`. `ClaudeCodeProvider` plugs that into TypeSafe's adapter at the same
seam as `AgentProvider`; the support agent, baseline, classifier and casebook call the runner directly. Traces keep
their format; replay works per session.

**Tech Stack:** Python 3.12, asyncio subprocesses, FastAPI app unchanged, pytest (`asyncio_mode = auto`), Claude Code
CLI 2.1.x, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-09-30-claude-code-backend-design.md`

## Global Constraints

- Scope: development and evaluation only; production config and image are unchanged unless `CLAUDE_CODE_VERSION` is set.
- Every `claude` invocation carries: `-p`, `--system-prompt-file`, `--model`, `--tools ""`, `--strict-mcp-config`,
  `--setting-sources ""`, `--json-schema`, `--output-format stream-json`, `--verbose`, `--max-turns`; stdin is the
  prompt; working directory is an empty temporary directory; `HOME` is not changed.
- `--no-session-persistence` on every session except a PACDS investigation (which may be resumed once for max turns).
- `--bare` is never used (it cannot use subscription auth).
- Tools only via our MCP server (`--allowedTools mcp__pacds__<name>` for each); never Claude Code's built-in tools.
- Authentication: host login, or `CLAUDE_CODE_OAUTH_TOKEN` in containers; never logged, traced or returned.
- Live verification uses the cheapest model: `haiku`.
- Commit messages: `<type>: <short description>`, types feat/fix/refactor/chore/docs/build/test, no mention of Claude
  Code's author or any assistant, no attribution lines. ("Claude Code" as the name of the CLI we integrate is fine.)
- Tests: `uv run pytest tests/unit -q` must stay green after every task; check the exit code directly, not via a pipe.

## File Structure

| File | Responsibility |
|---|---|
| `src/pacds/config.py` (modify) | `api: claude_code`; `base_url`/`api_key` optional for it |
| `src/pacds/engine/mcp_http.py` (create) | Minimal MCP streamable-HTTP server on 127.0.0.1; `Toolset`; `serve()` context |
| `src/pacds/engine/claude_code.py` (create) | Command line, subprocess, stream parsing → `Result`; `ClaudeCodeError`; `ask_json` |
| `src/pacds/engine/trace.py` (modify) | `Trace.add_session(...)` |
| `src/pacds/engine/replay.py` (modify) | Sessions indexed by hash; `Recordings.take_session` |
| `src/pacds/engine/tools.py` (modify) | `WorkspaceTools.fingerprint()` |
| `src/pacds/engine/claude_code_provider.py` (create) | `ClaudeCodeProvider` (adapter provider) |
| `src/pacds/engine/evaluator.py` (modify) | Choose the provider by `llm.api` |
| `src/pacds/devtools/check_llm.py` (modify) | `claude_code` checks |
| `tests/unit/fake_claude.py` (create) | Fake `claude` executable replaying canned stream-json |
| `tests/replay/baseline.py` (modify) | Baseline via `ClaudeCodeProvider` |
| `tests/support_agent/agent.py`, `run.py` (modify) | Support agent session |
| `tests/analysis/classify.py`, `tests/replay/casebook.py` (modify) | Single structured calls |
| `Dockerfile`, `compose.yaml`, `scripts/eval.sh`, `.env.example`, `pyproject.toml` (modify) | Image, env, guard, marker |
| `docs/evaluation-runbook.md`, `README.md` (modify) | Operator docs |

---

### Task 1: Configuration accepts `api: claude_code`

**Files:**
- Modify: `src/pacds/config.py` (`LLMConfig`)
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Produces: `LLMConfig.api` literal includes `"claude_code"`; `base_url: str = ""`, `api_key: str = ""`, required
  (non-empty) unless `api == "claude_code"`.

- [ ] **Step 1: Write the failing tests** (append to `tests/unit/test_config.py`)

```python
import pytest
from pydantic import ValidationError

from pacds.config import LLMConfig


def test_claude_code_needs_no_endpoint_or_key():
    llm = LLMConfig.model_validate({"model": "haiku", "api": "claude_code"})
    assert llm.api == "claude_code" and llm.base_url == "" and llm.api_key == ""


def test_other_apis_still_need_endpoint_and_key():
    with pytest.raises(ValidationError, match="base_url"):
        LLMConfig.model_validate({"model": "m", "api_key": "k"})
    with pytest.raises(ValidationError, match="api_key"):
        LLMConfig.model_validate({"model": "m", "base_url": "http://llm/v1", "api": "responses"})
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_config.py -q -k "claude_code or still_need"`
Expected: FAIL (`claude_code` is not an allowed literal; missing `base_url` passes validation differently).

- [ ] **Step 3: Implement**

In `LLMConfig`:

```python
class LLMConfig(_Strict):
    # Required unless api is claude_code, which runs the Claude Code CLI on its own login.
    base_url: str = ""
    model: str
    api_key: str = ""
    ...
    # Wire protocol: some models (e.g. OpenAI GPT on OpenCode Go) are only served on /responses. claude_code runs each
    # investigation as one `claude -p` session (pacds.engine.claude_code); base_url, api_key, session_header,
    # max_output_tokens, extra_body, final_keeps_tools and context_budget_tokens do not apply to it.
    api: Literal["chat_completions", "responses", "anthropic", "claude_code"] = "chat_completions"
```

and a model validator after the field validators:

```python
    @model_validator(mode="after")
    def _endpoint_unless_claude_code(self) -> LLMConfig:
        if self.api != "claude_code":
            for name in ("base_url", "api_key"):
                if not getattr(self, name):
                    raise ValueError(f"llm.{name} is required unless llm.api is claude_code")
        return self
```

- [ ] **Step 4: Run the unit suite**

Run: `uv run pytest tests/unit -q; echo exit=$?`
Expected: `exit=0`. If an existing test built an `LLMConfig` without `base_url`/`api_key` it now fails with the new
message — give it the values it implicitly relied on rather than weakening the validator.

- [ ] **Step 5: Commit**

```bash
git add src/pacds/config.py tests/unit/test_config.py
git commit -m "feat: accept llm.api claude_code without an endpoint or key"
```

---

### Task 2: In-process MCP server over HTTP

**Files:**
- Create: `src/pacds/engine/mcp_http.py`
- Test: `tests/unit/test_mcp_http.py`

**Interfaces:**
- Produces:
  - `Toolset(definitions: list[dict], call: Callable[[str, dict], Awaitable[str]])` — `definitions` are chat-completions
    function definitions (`{"type": "function", "function": {"name", "description", "parameters"}}`), as
    `WorkspaceTools.definitions` and `CALL_PACDS_TOOL` already are.
  - `SERVER_NAME = "pacds"`; tool names seen by Claude Code are `mcp__pacds__<name>`.
  - `async with serve(toolset) as mcp_config:` yields `{"mcpServers": {"pacds": {"type": "http", "url": ...}}}` and
    unregisters on exit. One server per event loop, started on first use, bound to `127.0.0.1` on a free port.
  - `allowed_tools(toolset) -> list[str]` → `["mcp__pacds__search_code", ...]`.

- [ ] **Step 1: Write the failing tests**

```python
import json

import httpx
import pytest

from pacds.engine.mcp_http import Toolset, allowed_tools, serve

DEFS = [{"type": "function", "function": {"name": "echo", "description": "Echo text.",
                                          "parameters": {"type": "object", "properties": {"text": {"type": "string"}},
                                                         "required": ["text"]}}}]


async def _echo(name: str, arguments: dict) -> str:
    return f"error: bad" if arguments.get("text") == "fail" else f"{name}:{arguments['text']}"


async def _post(url: str, message: dict) -> httpx.Response:
    async with httpx.AsyncClient() as client:
        return await client.post(url, json=message, headers={"Accept": "application/json, text/event-stream"})


async def test_initialize_list_and_call():
    async with serve(Toolset(DEFS, _echo)) as config:
        url = config["mcpServers"]["pacds"]["url"]
        assert url.startswith("http://127.0.0.1:")
        init = (await _post(url, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})).json()
        assert init["result"]["protocolVersion"] == "2025-06-18"
        assert init["result"]["capabilities"] == {"tools": {}}
        assert (await _post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"})).status_code == 202
        tools = (await _post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})).json()["result"]["tools"]
        assert tools == [{"name": "echo", "description": "Echo text.", "inputSchema": DEFS[0]["function"]["parameters"]}]
        call = (await _post(url, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "echo", "arguments": {"text": "hi"}}})).json()
        assert call["result"] == {"content": [{"type": "text", "text": "echo:hi"}], "isError": False}
        failed = (await _post(url, {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "echo", "arguments": {"text": "fail"}}})).json()
        assert failed["result"]["isError"] is True


async def test_unknown_token_method_and_verbs():
    async with serve(Toolset(DEFS, _echo)) as config:
        url = config["mcpServers"]["pacds"]["url"]
        base = url.rsplit("/", 1)[0]
        assert (await _post(base + "/not-a-token", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})).status_code == 404
        unknown = (await _post(url, {"jsonrpc": "2.0", "id": 5, "method": "resources/list"})).json()
        assert unknown["error"]["code"] == -32601
        async with httpx.AsyncClient() as client:
            assert (await client.get(url)).status_code == 405
    # the server lives on with the event loop; the session's path is gone
    assert (await _post(url, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})).status_code == 404


async def test_two_toolsets_are_isolated():
    async def other(name: str, arguments: dict) -> str:
        return "other"
    async with serve(Toolset(DEFS, _echo)) as a, serve(Toolset(DEFS, other)) as b:
        ua, ub = a["mcpServers"]["pacds"]["url"], b["mcpServers"]["pacds"]["url"]
        assert ua != ub
        msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo", "arguments": {"text": "x"}}}
        assert (await _post(ua, msg)).json()["result"]["content"][0]["text"] == "echo:x"
        assert (await _post(ub, msg)).json()["result"]["content"][0]["text"] == "other"


def test_allowed_tools():
    assert allowed_tools(Toolset(DEFS, _echo)) == ["mcp__pacds__echo"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_mcp_http.py -q`
Expected: FAIL with `ModuleNotFoundError: pacds.engine.mcp_http`.

- [ ] **Step 3: Implement `src/pacds/engine/mcp_http.py`**

```python
"""A minimal MCP server over streamable HTTP on localhost, so a `claude -p` session calls tools that run in this process.

It implements what Claude Code needs of the MCP specification (2025-06-18): initialize, ping, tools/list and
tools/call as POSTed JSON-RPC answered with JSON; notifications get 202; GET and DELETE get 405 (no server-initiated
stream, no sessions). Each session registers a Toolset under an unguessable path and unregisters it when done, so a
session reaches only its own tools. Written against the specification, not the `mcp` SDK, to keep a small surface.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

SERVER_NAME = "pacds"
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class Toolset:
    """Chat-completions function definitions and the coroutine that runs one call by name."""

    definitions: list[dict[str, Any]]
    call: Callable[[str, dict[str, Any]], Awaitable[str]]


def allowed_tools(toolset: Toolset) -> list[str]:
    return [f"mcp__{SERVER_NAME}__{d['function']['name']}" for d in toolset.definitions]


class _Server:
    def __init__(self) -> None:
        self._toolsets: dict[str, Toolset] = {}
        self._server: asyncio.Server | None = None
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    def register(self, toolset: Toolset) -> str:
        token = secrets.token_urlsafe(24)
        self._toolsets[token] = toolset
        return token

    def unregister(self, token: str) -> None:
        self._toolsets.pop(token, None)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            status, body = await self._respond(reader)
        except Exception:  # noqa: BLE001 - a malformed request must not stop the server
            logger.exception("mcp request failed")
            status, body = 400, None
        payload = b"" if body is None else json.dumps(body).encode()
        head = [f"HTTP/1.1 {status} {_REASONS.get(status, 'Error')}", f"Content-Length: {len(payload)}", "Connection: close"]
        if body is not None:
            head.append("Content-Type: application/json")
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode() + payload)
        try:
            await writer.drain()
        finally:
            writer.close()

    async def _respond(self, reader: asyncio.StreamReader) -> tuple[int, Any]:
        request_line = (await reader.readline()).decode("latin-1").split()
        headers: dict[str, str] = {}
        while (line := (await reader.readline()).decode("latin-1").strip()):
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        if len(request_line) < 2:
            return 400, None
        method, path = request_line[0], request_line[1]
        toolset = self._toolsets.get(path.removeprefix("/mcp/")) if path.startswith("/mcp/") else None
        if toolset is None:
            return 404, None
        if method != "POST":
            return 405, None
        length = int(headers.get("content-length") or 0)
        if length > MAX_BODY_BYTES:
            return 413, None
        message = json.loads(await reader.readexactly(length))
        reply = await _dispatch(toolset, message)
        return (202, None) if reply is None else (200, reply)


_REASONS = {200: "OK", 202: "Accepted", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed", 413: "Payload Too Large"}


async def _dispatch(toolset: Toolset, message: dict[str, Any]) -> dict[str, Any] | None:
    if "id" not in message:  # a notification
        return None
    method, params = message.get("method"), message.get("params") or {}
    if method == "initialize":
        requested = params.get("protocolVersion")
        result: dict[str, Any] = {
            "protocolVersion": requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": "1"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": d["function"]["name"], "description": d["function"].get("description", ""),
                             "inputSchema": d["function"]["parameters"]} for d in toolset.definitions]}
    elif method == "tools/call":
        arguments = params.get("arguments")
        text = await toolset.call(params.get("name", ""), arguments if isinstance(arguments, dict) else {})
        result = {"content": [{"type": "text", "text": text}], "isError": text.startswith("error:")}
    else:
        return {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": message["id"], "result": result}


_servers: dict[asyncio.AbstractEventLoop, _Server] = {}


async def _server() -> _Server:
    loop = asyncio.get_running_loop()
    for stale in [other for other in _servers if other.is_closed()]:
        del _servers[stale]
    if loop not in _servers:
        server = _Server()
        await server.start()
        _servers[loop] = server
    return _servers[loop]


@asynccontextmanager
async def serve(toolset: Toolset) -> AsyncIterator[dict[str, Any]]:
    """Register `toolset` for one session; yields the --mcp-config object that reaches it."""
    server = await _server()
    token = server.register(toolset)
    try:
        yield {"mcpServers": {SERVER_NAME: {"type": "http", "url": f"http://127.0.0.1:{server.port}/mcp/{token}"}}}
    finally:
        server.unregister(token)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_mcp_http.py -q; echo exit=$?`
Expected: `exit=0`.

- [ ] **Step 5: Commit**

```bash
git add src/pacds/engine/mcp_http.py tests/unit/test_mcp_http.py
git commit -m "feat: serve in-process tools to Claude Code over local MCP"
```

---

### Task 3: Claude Code runner and fake CLI

**Files:**
- Create: `src/pacds/engine/claude_code.py`
- Create: `tests/unit/fake_claude.py`
- Create: `tests/unit/claude_streams/` (canned stream-json files, below)
- Test: `tests/unit/test_claude_code.py`
- Modify: `pyproject.toml` (marker `claude_code`), create `tests/live/__init__.py`, `tests/live/test_claude_code_live.py`

**Interfaces:**
- Consumes: `Toolset`, `serve`, `allowed_tools` (Task 2).
- Produces (in `pacds.engine.claude_code`):
  - `EXECUTABLE_ENV = "PACDS_CLAUDE_EXECUTABLE"` — overrides the `claude` executable (tests point it at the fake).
  - `@dataclass Turn(text: str, tool_calls: list[dict], usage: dict[str, int], model: str | None)` where each tool
    call is `{"id", "name", "arguments"}` (`name` without the `mcp__pacds__` prefix; `arguments` a JSON string).
  - `@dataclass Result(session_id, subtype, is_error, structured_output, turns: list[Turn], tool_results: dict[str, str],
    usage: dict[str, int], cost_usd: float | None, num_turns: int, text: str, latency_ms: int)` with
    `to_dict()` / `Result.from_dict(d)`.
  - `class ClaudeCodeError(Exception)` with `.kind` in `{"usage_limit", "auth", "failed"}`.
  - `command(*, system_file, model, schema, max_turns, effort=None, mcp_config=None, tools=(), resume=None, persist=False, executable="claude") -> list[str]`
  - `async def run(*, system, prompt, schema, model, max_turns, toolset=None, effort=None, resume=None, persist=False, cwd=None) -> Result`
    — `cwd` reuses a session's working directory (a resume must run where its session was created); `Result.cwd` holds it.
    — raises `ClaudeCodeError` for usage limits, auth failures and runs without a `result` event; returns a `Result`
    for `success` and for `error_max_turns` (the caller decides). Cancelling kills the process group.
  - `async def ask_json(*, system, prompt, schema, model, effort=None) -> tuple[dict, dict[str, int]]` — one call
    without tools (`max_turns=2`); raises `ClaudeCodeError("failed")` when there is no structured output.
  - `def forget_session(result: Result) -> None` — deletes a persisted session's working directory and transcript (best effort).

- [ ] **Step 1: Write the canned streams** (`tests/unit/claude_streams/*.jsonl`, one JSON event per line; shapes copied
  from a real `claude -p --output-format stream-json --verbose` run)

`success.jsonl`:
```
{"type":"system","subtype":"init","session_id":"s-1","model":"claude-haiku-4-5","tools":["StructuredOutput","mcp__pacds__read_file"],"mcp_servers":[{"name":"pacds","status":"connected"}]}
{"type":"assistant","message":{"id":"m1","model":"claude-haiku-4-5","content":[{"type":"text","text":"Reading."}],"usage":{"input_tokens":5,"output_tokens":3,"cache_read_input_tokens":0,"cache_creation_input_tokens":900}},"session_id":"s-1"}
{"type":"assistant","message":{"id":"m1","model":"claude-haiku-4-5","content":[{"type":"tool_use","id":"t1","name":"mcp__pacds__read_file","input":{"path":"retry.py"}}],"usage":{"input_tokens":5,"output_tokens":20,"cache_read_input_tokens":0,"cache_creation_input_tokens":900}},"session_id":"s-1"}
{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"t1","content":"def retry(): pass"}]},"session_id":"s-1"}
{"type":"assistant","message":{"id":"m2","model":"claude-haiku-4-5","content":[{"type":"tool_use","id":"t2","name":"StructuredOutput","input":{"answers":{"q":"yes"}}}],"usage":{"input_tokens":2,"output_tokens":30,"cache_read_input_tokens":900,"cache_creation_input_tokens":50,"output_tokens_details":{"thinking_tokens":7}}},"session_id":"s-1"}
{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"t2","content":"Structured output provided successfully"}]},"session_id":"s-1"}
{"type":"result","subtype":"success","is_error":false,"session_id":"s-1","num_turns":3,"result":"{\"answers\":{\"q\":\"yes\"}}","structured_output":{"answers":{"q":"yes"}},"total_cost_usd":0.004,"api_error_status":null,"usage":{"input_tokens":7,"output_tokens":50,"cache_read_input_tokens":900,"cache_creation_input_tokens":950}}
```

`max_turns.jsonl`:
```
{"type":"system","subtype":"init","session_id":"s-2","model":"claude-haiku-4-5","tools":["StructuredOutput","mcp__pacds__read_file"]}
{"type":"assistant","message":{"id":"m1","model":"claude-haiku-4-5","content":[{"type":"tool_use","id":"t1","name":"mcp__pacds__read_file","input":{"path":"a.py"}}],"usage":{"input_tokens":5,"output_tokens":20,"cache_read_input_tokens":0,"cache_creation_input_tokens":900}},"session_id":"s-2"}
{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"t1","content":[{"type":"text","text":"x = 1"}]}]},"session_id":"s-2"}
{"type":"result","subtype":"error_max_turns","is_error":true,"session_id":"s-2","num_turns":2,"result":"","total_cost_usd":0.002,"api_error_status":null,"usage":{"input_tokens":5,"output_tokens":20,"cache_read_input_tokens":0,"cache_creation_input_tokens":900}}
```

`usage_limit.jsonl`:
```
{"type":"system","subtype":"init","session_id":"s-3","model":"claude-haiku-4-5","tools":["StructuredOutput"]}
{"type":"result","subtype":"success","is_error":true,"session_id":"s-3","num_turns":1,"result":"Claude AI usage limit reached|1790000000","total_cost_usd":0,"api_error_status":429,"usage":{"input_tokens":0,"output_tokens":0}}
```

`auth.jsonl` is empty; the fake prints `Invalid API key · Please run /login` to stderr and exits 1 for it.

- [ ] **Step 2: Write the fake executable `tests/unit/fake_claude.py`**

```python
#!/usr/bin/env python3
"""Fake `claude` for unit tests: records its argv and stdin, then replays a canned stream.

FAKE_CLAUDE_STREAM names a file in tests/unit/claude_streams (or a comma list: one per invocation, in order, tracked
in FAKE_CLAUDE_LOG's line count). FAKE_CLAUDE_LOG gets one JSON line per invocation: {"argv", "stdin", "cwd"}.
FAKE_CLAUDE_CALL_TOOL=name:json makes the fake call that MCP tool through --mcp-config (when given) before replaying,
and put the tool's text into the replayed tool_result of id t1. "auth.jsonl" exits 1 with a login error; "hang" sleeps forever.
"""

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

STREAMS = Path(__file__).parent / "claude_streams"


def main() -> None:
    argv = sys.argv[1:]
    stdin = sys.stdin.read()
    log = Path(os.environ["FAKE_CLAUDE_LOG"])
    previous = log.read_text().count("\n") if log.exists() else 0
    with log.open("a") as out:
        out.write(json.dumps({"argv": argv, "stdin": stdin, "cwd": os.getcwd()}) + "\n")
    streams = os.environ["FAKE_CLAUDE_STREAM"].split(",")
    stream = streams[min(previous, len(streams) - 1)]
    if stream == "hang":
        time.sleep(3600)
    if stream == "auth.jsonl":
        print("Invalid API key · Please run /login", file=sys.stderr)
        sys.exit(1)
    tool_text = None
    if os.environ.get("FAKE_CLAUDE_CALL_TOOL") and "--mcp-config" in argv:
        name, _, raw = os.environ["FAKE_CLAUDE_CALL_TOOL"].partition(":")
        config = json.loads(argv[argv.index("--mcp-config") + 1])
        url = config["mcpServers"]["pacds"]["url"]
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": json.loads(raw)}}).encode()
        request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        tool_text = json.loads(urllib.request.urlopen(request).read())["result"]["content"][0]["text"]
    for line in (STREAMS / stream).read_text().splitlines():
        event = json.loads(line)
        if tool_text is not None and event.get("type") == "user":
            for block in event["message"]["content"]:
                if block.get("tool_use_id") == "t1":
                    block["content"] = tool_text
        print(json.dumps(event), flush=True)


if __name__ == "__main__":
    main()
```

Make it executable: `chmod +x tests/unit/fake_claude.py`.

- [ ] **Step 3: Write the failing tests `tests/unit/test_claude_code.py`**

```python
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from pacds.engine import claude_code
from pacds.engine.claude_code import ClaudeCodeError, command
from pacds.engine.mcp_http import Toolset

FAKE = Path(__file__).parent / "fake_claude.py"
SCHEMA = {"type": "object", "properties": {"answers": {"type": "object"}}, "required": ["answers"]}
DEFS = [{"type": "function", "function": {"name": "read_file", "description": "Read.",
                                          "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}]


@pytest.fixture
def fake(tmp_path, monkeypatch):
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv(claude_code.EXECUTABLE_ENV, f"{sys.executable} {FAKE}")
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))

    def use(stream: str, tool: str | None = None) -> Path:
        monkeypatch.setenv("FAKE_CLAUDE_STREAM", stream)
        if tool:
            monkeypatch.setenv("FAKE_CLAUDE_CALL_TOOL", tool)
        return log
    return use


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


async def test_success_parses_turns_tools_and_usage(fake):
    log = fake("success.jsonl")
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


async def test_max_turns_is_returned_for_the_caller(fake):
    fake("max_turns.jsonl")
    result = await claude_code.run(system="s", prompt="p", schema=SCHEMA, model="haiku", max_turns=1)
    assert result.subtype == "error_max_turns" and result.structured_output is None
    assert result.tool_results == {"t1": "x = 1"}  # list-of-blocks content flattened


async def test_usage_limit_and_auth_raise(fake):
    fake("usage_limit.jsonl")
    with pytest.raises(ClaudeCodeError) as limit:
        await claude_code.run(system="s", prompt="p", schema=SCHEMA, model="haiku", max_turns=1)
    assert limit.value.kind == "usage_limit"
    fake("auth.jsonl")
    with pytest.raises(ClaudeCodeError) as auth:
        await claude_code.run(system="s", prompt="p", schema=SCHEMA, model="haiku", max_turns=1)
    assert auth.value.kind == "auth" and "login" in str(auth.value)


async def test_tools_are_served_over_mcp(fake):
    calls = []

    async def call(name: str, arguments: dict) -> str:
        calls.append((name, arguments))
        return "served text"
    fake("success.jsonl", tool='read_file:{"path": "retry.py"}')
    result = await claude_code.run(system="s", prompt="p", schema=SCHEMA, model="haiku", max_turns=5, toolset=Toolset(DEFS, call))
    assert calls == [("read_file", {"path": "retry.py"})]
    assert result.tool_results["t1"] == "served text"


async def test_cancel_kills_the_process(fake):
    fake("hang")
    task = asyncio.create_task(claude_code.run(system="s", prompt="p", schema=SCHEMA, model="haiku", max_turns=1))
    await asyncio.sleep(1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_ask_json(fake):
    fake("success.jsonl")
    answer, usage = await claude_code.ask_json(system="s", prompt="p", schema=SCHEMA, model="haiku")
    assert answer == {"answers": {"q": "yes"}} and usage["output"] == 50


def test_result_round_trips():
    result = claude_code.Result(session_id="s", subtype="success", is_error=False, structured_output={"a": 1},
                                turns=[claude_code.Turn(text="t", tool_calls=[], usage={"input": 1}, model="m")],
                                tool_results={}, usage={"input": 1}, cost_usd=0.1, num_turns=1, text="", latency_ms=5)
    assert claude_code.Result.from_dict(json.loads(json.dumps(result.to_dict()))) == result
```

- [ ] **Step 4: Run to verify they fail**

Run: `uv run pytest tests/unit/test_claude_code.py -q`
Expected: FAIL with `ModuleNotFoundError: pacds.engine.claude_code`.

- [ ] **Step 5: Implement `src/pacds/engine/claude_code.py`**

```python
"""Run one isolated Claude Code session (`claude -p`) with our tools and a JSON-schema answer.

For llm.api claude_code: a Claude subscription runs the Claude Code CLI but gives no API key. The session sees only the
caller's tools (served in-process over MCP, pacds.engine.mcp_http) and Claude Code's StructuredOutput; built-in tools,
settings, CLAUDE.md, plugins and MCP servers from the environment are all off. The prompt goes in on stdin (an argument
is limited to 128 KiB), the system prompt through a file in an empty temporary working directory.
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
    cwd: str = field(default="", compare=False)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("cwd")
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Result:
        return cls(**{**data, "turns": [Turn(**turn) for turn in data["turns"]]})


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


def command(*, system_file: str, model: str, schema: dict[str, Any], max_turns: int, effort: str | None = None,
            mcp_config: dict[str, Any] | None = None, tools: list[str] | tuple[str, ...] = (), resume: str | None = None,
            persist: bool = False, executable: list[str] | None = None) -> list[str]:
    argv = [*(executable or ["claude"]), "-p", "--system-prompt-file", system_file, "--model", model,
            "--tools", "", "--strict-mcp-config", "--setting-sources", "",
            "--json-schema", json.dumps(schema), "--output-format", "stream-json", "--verbose",
            "--max-turns", str(max_turns)]
    if mcp_config is not None:
        argv += ["--mcp-config", json.dumps(mcp_config)]
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

    def feed(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        self.session_id = event.get("session_id") or self.session_id
        if kind == "assistant":
            message = event.get("message") or {}
            turn = self.turns.setdefault(message.get("id") or f"turn-{len(self.turns)}", Turn("", [], {}, message.get("model")))
            for block in message.get("content") or []:
                if block.get("type") == "text":
                    turn.text += block.get("text", "")
                elif block.get("type") == "tool_use" and block.get("name") != STRUCTURED_OUTPUT_TOOL:
                    turn.tool_calls.append({"id": block.get("id"), "name": (block.get("name") or "").removeprefix(_TOOL_PREFIX),
                                            "arguments": json.dumps(block.get("input") or {})})
            if message.get("usage"):
                turn.usage = _usage(message["usage"])  # repeated per content block of one message: the last one wins
        elif kind == "user":
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id"):
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
              effort: str | None = None, resume: str | None = None, persist: bool = False, cwd: str | None = None) -> Result:
    """One session. `cwd` reuses a working directory (a resume must run where its session was created)."""
    started = time.monotonic()
    owned = cwd is None
    workdir = cwd or tempfile.mkdtemp(prefix="pacds-claude-")
    try:
        system_file = Path(workdir) / "system-prompt.txt"
        system_file.write_text(system)
        if toolset is None:
            return await _session(command(system_file=str(system_file), model=model, schema=schema, max_turns=max_turns, effort=effort,
                                          resume=resume, persist=persist, executable=executable()), prompt, workdir, started)
        async with serve(toolset) as mcp_config:
            argv = command(system_file=str(system_file), model=model, schema=schema, max_turns=max_turns, effort=effort,
                           mcp_config=mcp_config, tools=allowed_tools(toolset), resume=resume, persist=persist, executable=executable())
            return await _session(argv, prompt, workdir, started)
    finally:
        if owned and not persist:
            _remove(workdir)


async def _session(argv: list[str], prompt: str, cwd: str, started: float) -> Result:
    process = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True, limit=STREAM_LIMIT_BYTES)
    stream = _Stream()
    try:
        assert process.stdin and process.stdout and process.stderr
        process.stdin.write(prompt.encode())
        await process.stdin.drain()
        process.stdin.close()
        stderr_task = asyncio.create_task(process.stderr.read())
        async for line in process.stdout:
            if line.strip():
                try:
                    stream.feed(json.loads(line))
                except json.JSONDecodeError:
                    continue
        await process.wait()
        stderr = (await stderr_task).decode(errors="replace")[-2000:]
    except BaseException:  # cancelled by a time budget, or anything else: never leave the CLI running
        _kill(process)
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


def _kill(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _remove(path: str) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def forget_session(result: Result) -> None:
    """Delete a persisted session's working directory and transcript (~/.claude/projects/<cwd with - for />/)."""
    if not result.cwd:
        return
    _remove(result.cwd)
    _remove(str(Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", result.cwd)))


async def ask_json(*, system: str, prompt: str, schema: dict[str, Any], model: str, effort: str | None = None) -> tuple[dict[str, Any], dict[str, int]]:
    """One answer without tools (baseline-style clients: classifier, casebook reviews)."""
    result = await run(system=system, prompt=prompt, schema=schema, model=model, max_turns=2, effort=effort)
    if not isinstance(result.structured_output, dict):
        raise ClaudeCodeError("failed", f"no structured output ({result.subtype})")
    return result.structured_output, result.usage
```

- [ ] **Step 6: Run the unit tests**

Run: `uv run pytest tests/unit/test_claude_code.py -q; echo exit=$?`
Expected: `exit=0`.

- [ ] **Step 7: Add the live test (subscription, haiku) and its marker**

In `pyproject.toml` `[tool.pytest.ini_options]`: add the marker
`"claude_code: runs the real Claude Code CLI on the local login with haiku (costs subscription usage)"` and change
`addopts` to `"-m 'not e2e and not llm and not claude_code'"`.

`tests/live/__init__.py`: empty. `tests/live/test_claude_code_live.py`:

```python
"""Live checks of the Claude Code backend against the real CLI (haiku). Run: uv run pytest -m claude_code tests/live"""

import pytest

from pacds.engine import claude_code
from pacds.engine.mcp_http import Toolset

pytestmark = pytest.mark.claude_code
SCHEMA = {"type": "object", "properties": {"retries": {"type": "integer"}}, "required": ["retries"], "additionalProperties": False}
DEFS = [{"type": "function", "function": {"name": "read_file", "description": "Read a file from the repository.",
                                          "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}]


async def test_http_mcp_tool_and_schema_answer():
    calls = []

    async def call(name: str, arguments: dict) -> str:
        calls.append((name, arguments))
        return "def retry():\n    # retries 3 times then gives up\n    pass\n"
    result = await claude_code.run(system="You investigate code with the given tools. Always read the file before answering.",
                                   prompt="How many times does retry() in retry.py retry?", schema=SCHEMA, model="haiku",
                                   max_turns=5, toolset=Toolset(DEFS, call))
    assert calls and calls[0][0] == "read_file"
    assert result.structured_output == {"retries": 3}
    assert result.usage["input"] > 0


async def test_max_turns_then_resume():
    async def call(name: str, arguments: dict) -> str:
        return "def retry():\n    # retries 3 times\n"
    first = await claude_code.run(system="Read the file with the tool before answering.", prompt="How many retries in retry.py?",
                                  schema=SCHEMA, model="haiku", max_turns=1, toolset=Toolset(DEFS, call), persist=True)
    try:
        assert first.subtype == "error_max_turns"
        second = await claude_code.run(system="Read the file with the tool before answering.", prompt="Answer now from what you read.",
                                       schema=SCHEMA, model="haiku", max_turns=2, toolset=Toolset(DEFS, call), resume=first.session_id,
                                       persist=True, cwd=first.cwd)
        assert second.structured_output == {"retries": 3}
    finally:
        claude_code.forget_session(first)
```

Run: `uv run pytest -m claude_code tests/live -q; echo exit=$?`
Expected: `exit=0`. This is the first proof that Claude Code accepts our HTTP MCP server and that resume works. If the
HTTP MCP server shows `status: failed`, capture the CLI's debug output (`claude --debug mcp ...` with the same flags) and
fix `mcp_http.py` against it before going on — do not switch to stdio silently; report the finding.
If a resumed session misses the MCP tool or the session, check `first.cwd` still exists (persist keeps it) and report.

- [ ] **Step 8: Commit**

```bash
chmod +x tests/unit/fake_claude.py
git add src/pacds/engine/claude_code.py tests/unit/fake_claude.py tests/unit/claude_streams tests/unit/test_claude_code.py \
        tests/live pyproject.toml
git commit -m "feat: run isolated Claude Code sessions with MCP tools and schema answers"
```

---

### Task 4: Session traces and session replay

**Files:**
- Modify: `src/pacds/engine/trace.py` (`Trace.add_session`)
- Modify: `src/pacds/engine/replay.py` (`Recordings.add`, `Recordings.take_session`)
- Modify: `src/pacds/engine/tools.py` (`WorkspaceTools.fingerprint`)
- Test: `tests/unit/test_trace_sessions.py`, `tests/unit/test_tools.py`

**Interfaces:**
- Consumes: `Result`, `Turn` (Task 3).
- Produces:
  - `SESSION_PHASE = "claude_code"` in `trace.py`.
  - `Trace.add_session(result: Result, *, request_sha256: str, label: str, replayed: bool = False, tools_from_result: bool = False) -> None`:
    appends one `calls` entry per turn (`phase = "claude_code"`, `kept`/`messages_added` so `messages_at` rebuilds the
    transcript, `request_sha256: None` so per-call replay ignores them, `response = {"content", "tool_calls",
    "finish_reason": None, "model"}`, `usage`, `latency_ms: None`, `attempts: []`, `replayed` when replayed), and one
    `info["sessions"]` entry `{"label", "request_sha256", "session_id", "subtype", "num_turns", "cost_usd", "usage",
    "latency_ms", "replayed", "result": result.to_dict()}`. With `tools_from_result`, it also appends `tools` entries
    rebuilt from the turns' tool calls and `result.tool_results` (latency 0), for replayed sessions.
  - `Recordings.take_session(request_sha256: str) -> Result | None`: a recorded session with that hash whose result has
    a structured output; each recording is served once, like calls.
  - `async WorkspaceTools.fingerprint() -> dict` → `{"commit": "<sha>", "logs": {"<name>": "<sha256>"}}`.

- [ ] **Step 1: Write the failing tests** (`tests/unit/test_trace_sessions.py`)

```python
from pacds.engine.claude_code import Result, Turn
from pacds.engine.replay import Recordings
from pacds.engine.trace import SESSION_PHASE, Trace, messages_at


def _result(output=None) -> Result:
    return Result(session_id="s-1", subtype="success", is_error=False, structured_output=output if output is not None else {"a": 1},
                  turns=[Turn("Reading.", [{"id": "t1", "name": "read_file", "arguments": '{"path": "x"}'}], {"input": 10, "output": 2, "cached": 0}, "m"),
                         Turn("", [], {"input": 12, "output": 5, "cached": 10}, "m")],
                  tool_results={"t1": "body"}, usage={"input": 22, "output": 7, "cached": 10}, cost_usd=0.01, num_turns=3, text="", latency_ms=900)


def test_add_session_writes_turns_and_session():
    trace = Trace(request_id="r")
    trace.add_session(_result(), request_sha256="h", label="investigate")
    assert [c["phase"] for c in trace.calls] == [SESSION_PHASE, SESSION_PHASE]
    assert all(c["request_sha256"] is None for c in trace.calls)
    assert trace.calls[0]["response"]["tool_calls"] == [{"id": "t1", "name": "read_file", "arguments": '{"path": "x"}'}]
    assert messages_at(trace.calls, 2)[-1] == {"role": "tool", "tool_call_id": "t1", "content": "body"}
    session = trace.info["sessions"][0]
    assert session["request_sha256"] == "h" and session["cost_usd"] == 0.01 and session["replayed"] is False
    assert trace.usage()["input"] == 22 and trace.tools == []


def test_replayed_session_rebuilds_tools():
    trace = Trace()
    trace.add_session(_result(), request_sha256="h", label="investigate", replayed=True, tools_from_result=True)
    assert all(c["replayed"] for c in trace.calls)
    assert trace.tools[0]["name"] == "read_file" and trace.tools[0]["result"] == "body"
    assert trace.usage()["replayed_calls"] == 2


def test_recordings_serve_sessions_once():
    trace = Trace()
    trace.add_session(_result(), request_sha256="h", label="investigate")
    recordings = Recordings([trace.to_dict()])
    assert recordings.take_session("h") == _result()
    assert recordings.take_session("h") is None
    assert recordings.take_session("other") is None


def test_sessions_without_answer_are_not_recorded():
    trace = Trace()
    failed = _result()
    failed.structured_output = None
    trace.add_session(failed, request_sha256="h", label="investigate")
    assert Recordings([trace.to_dict()]).take_session("h") is None
```

Append to `tests/unit/test_tools.py` (use the module's existing git-repo fixture; if it is named differently, adapt the
fixture name, not the assertions):

```python
async def test_fingerprint_names_commit_and_log_digests(tmp_path):
    import hashlib
    import subprocess

    repo, logs = tmp_path / "repo", tmp_path / "logs"
    repo.mkdir(); logs.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "a.txt").write_text("a")
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "x", "--allow-empty"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "a"], cwd=repo, check=True)
    (logs / "server.log").write_text("line")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    fingerprint = await WorkspaceTools(repo, logs).fingerprint()
    assert fingerprint == {"commit": head, "logs": {"server.log": hashlib.sha256(b"line").hexdigest()}}
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_trace_sessions.py tests/unit/test_tools.py -q -k "session or fingerprint"`
Expected: FAIL (`SESSION_PHASE`, `add_session`, `take_session`, `fingerprint` missing).

- [ ] **Step 3: Implement**

`src/pacds/engine/trace.py` — add after `add_tool`:

```python
SESSION_PHASE = "claude_code"  # one call entry per assistant turn of a Claude Code session (pacds.engine.claude_code)


    def add_session(self, result: Any, *, request_sha256: str, label: str, replayed: bool = False, tools_from_result: bool = False) -> None:
        """Record a Claude Code session: its turns as calls (for usage and transcripts) and the session for replay.

        The turns carry no request hash: a session is replayed whole (Recordings.take_session), never call by call.
        As for model calls, turn n's messages are turn n-1's messages plus turn n-1's reply and its tool results."""
        sent = messages_at(self.calls, len(self.calls))
        previous = self.calls[-1]["response"] if self.calls else None
        for turn in result.turns:
            added: list[dict[str, Any]] = []
            if previous is not None:
                added.append({"role": "assistant", "content": previous["content"], "tool_calls": previous["tool_calls"]})
                added += [{"role": "tool", "tool_call_id": call["id"], "content": result.tool_results.get(call["id"], "")}
                          for call in previous["tool_calls"]]
            data: dict[str, Any] = {
                "n": len(self.calls) + 1, "phase": SESSION_PHASE, "kept": len(sent), "messages_added": added, "request_sha256": None,
                "response": {"content": turn.text, "tool_calls": turn.tool_calls, "finish_reason": None, "model": turn.model},
                "usage": turn.usage, "latency_ms": None, "attempts": [],
            }
            if replayed:
                data["replayed"] = True
            self.calls.append(data)
            sent, previous = sent + added, data["response"]
            if tools_from_result:
                for call in turn.tool_calls:
                    text = result.tool_results.get(call["id"], "")
                    self.tools.append({"call": data["n"], "name": call["name"], "arguments": call["arguments"], "result": text,
                                       "result_chars": len(text), "latency_ms": 0, "error": text.startswith("error:")})
        self.info.setdefault("sessions", []).append({
            "label": label, "request_sha256": request_sha256, "session_id": result.session_id, "subtype": result.subtype,
            "num_turns": result.num_turns, "cost_usd": result.cost_usd, "usage": result.usage, "latency_ms": result.latency_ms,
            "replayed": replayed, "result": result.to_dict(),
        })
```

`messages_at` rebuilds call n as call n-1's first `kept` messages plus `messages_added`; here `kept` is everything
call n-1 had, and `messages_added` is call n-1's reply plus its tool results, so `messages_at(trace.calls, 2)` ends with
the tool result of turn 1. The session's initial prompt is not repeated in the calls: it is in the request (PACDS traces
already hold `questions` and `document`).

`src/pacds/engine/replay.py` — in `Recordings.__init__` add `self._sessions: dict[str, deque[dict[str, Any]]] = {}`;
at the end of `add`:

```python
        for session in trace.get("sessions") or []:
            result = session.get("result") or {}
            if session.get("request_sha256") and result.get("structured_output") is not None:
                self._sessions.setdefault(session["request_sha256"], deque()).append(result)
                self.recorded += 1
```

and a method:

```python
    def take_session(self, request_sha256: str) -> Any:
        """A recorded Claude Code session (pacds.engine.claude_code.Result) for this request hash, once; else None."""
        from pacds.engine.claude_code import Result

        recorded = self._sessions.get(request_sha256)
        return Result.from_dict(recorded.popleft()) if recorded else None
```

Update the module docstring's first paragraph with one sentence: "Claude Code sessions (llm.api claude_code) are
recorded and replayed whole, by the hash of the session's request."

`src/pacds/engine/tools.py` — add `import hashlib` and to `WorkspaceTools`:

```python
    async def fingerprint(self) -> dict[str, Any]:
        """What every tool result depends on: the checkout's commit and each log file's digest (replay keys)."""
        commit = (await _run([*_GIT, "rev-parse", "HEAD"], self._repo))[0]
        logs = ({path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(self._logs.iterdir()) if path.is_file()}
                if self._logs.is_dir() else {})
        return {"commit": commit, "logs": logs}
```

- [ ] **Step 4: Run the unit suite**

Run: `uv run pytest tests/unit -q; echo exit=$?`
Expected: `exit=0`.

- [ ] **Step 5: Commit**

```bash
git add src/pacds/engine/trace.py src/pacds/engine/replay.py src/pacds/engine/tools.py tests/unit/test_trace_sessions.py tests/unit/test_tools.py
git commit -m "feat: trace and replay Claude Code sessions whole"
```

---

### Task 5: `ClaudeCodeProvider` in PACDS

**Files:**
- Create: `src/pacds/engine/claude_code_provider.py`
- Modify: `src/pacds/engine/evaluator.py`
- Test: `tests/unit/test_claude_code_provider.py`, and one case in `tests/unit/test_evaluator.py`

**Interfaces:**
- Consumes: `claude_code.run`, `Result`, `ClaudeCodeError`, `forget_session` (Task 3); `Toolset` (Task 2);
  `Trace.add_session`, `Recordings.take_session`, `WorkspaceTools.fingerprint` (Task 4); from `agent_provider`:
  `AGENT_SYSTEM_PROMPT`, `FINAL_INSTRUCTION`, `AgentBudgetExceeded`; `describe_questions`.
- Produces: `ClaudeCodeProvider(*, model_name, tools: WorkspaceTools | None, max_turns, time_budget_seconds, effort=None,
  trace=None, replay=None, system_prompt=AGENT_SYSTEM_PROMPT)` implementing the adapter's `AsyncProvider`
  (`model_name`, `request(messages, *, schema, structured) -> ProviderResult`, `translate_error`). Task 7 reuses it for
  the baseline with `tools=None` and `system_prompt=BASELINE_SYSTEM_PROMPT`.

Behavior:
1. First `request`: prompt = `describe_questions(schema)` + `"\n\n"` + the adapter's non-system messages' contents
   joined by blank lines (the same content `AgentProvider._investigate` sends as separate messages). Request hash =
   `sha256({"api": "claude_code", "model", "effort", "system", "prompt", "schema", "tools": definitions or [],
   "max_turns", "workspace": await tools.fingerprint() if tools else None})`.
2. Replay hit → `trace.add_session(result, replayed=True, tools_from_result=True)`; use its output.
3. Live: inside `asyncio.timeout(time_budget_seconds)`, `run(..., max_turns=max_turns + 1, toolset=Toolset(definitions,
   traced_call), persist=True)` (the `+ 1` is the `StructuredOutput` turn). `traced_call` runs `tools.call` and
   `trace.add_tool` (as `AgentProvider` does) and logs `agent tool request=... tool=... args=... result_chars=...`.
   On `error_max_turns`: record that session, then resume once with prompt `FINAL_INSTRUCTION`, `max_turns=2`,
   `resume=session_id`, `cwd=first.cwd`, `persist=True`. Always `forget_session` both in `finally`.
   No structured output after that → `TypeSafeError("claude code session ended without an answer: <subtype>")`.
   Budget expiry → `trace.info["investigation"] = {"turns": ..., "reason": "time_budget"}` and `AgentBudgetExceeded`.
   Otherwise `trace.info["investigation"] = {"turns": num_turns, "reason": "answered" | "turn_budget_resumed"}`.
4. Later `request` calls (adapter corrections): a fresh session without tools, no persistence, prompt = original prompt
   + `"\n\nYour previous answer:\n" + previous_text + "\n\n"` + the correction messages' contents; replayed by its own hash.
5. `ClaudeCodeError` → `translate_error`: `usage_limit` → `TypeSafeAPIError(529, None, httpx2.Headers(), message=...)`,
   `auth` / `failed` → `TypeSafeError(message)`. Existing `TypeSafeError`s pass through unchanged.
6. Returns `ProviderResult(text=json.dumps(structured_output), input_tokens=usage["input"], output_tokens=usage["output"])`,
   summing both sessions when resumed; `trace.info["final_raw"]` = that text.

- [ ] **Step 1: Write the failing tests** (`tests/unit/test_claude_code_provider.py`)

```python
import json
import sys
from pathlib import Path

import pytest
from system_one_adapter.providers.base import Message
from typesafe_sdk import TypeSafeAPIError, TypeSafeError

from pacds.engine import claude_code
from pacds.engine.agent_provider import AgentBudgetExceeded
from pacds.engine.claude_code_provider import ClaudeCodeProvider
from pacds.engine.replay import Recordings
from pacds.engine.trace import Trace

FAKE = Path(__file__).parent / "fake_claude.py"
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


@pytest.fixture
def fake(tmp_path, monkeypatch):
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv(claude_code.EXECUTABLE_ENV, f"{sys.executable} {FAKE}")
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))

    def use(streams: str, tool: str | None = None):
        monkeypatch.setenv("FAKE_CLAUDE_STREAM", streams)
        if tool:
            monkeypatch.setenv("FAKE_CLAUDE_CALL_TOOL", tool)
        return lambda: [json.loads(line) for line in log.read_text().splitlines()]
    return use


def _provider(tools=None, trace=None, replay=None, budget=30.0):
    return ClaudeCodeProvider(model_name="haiku", tools=tools if tools is not None else Tools(), max_turns=4,
                              time_budget_seconds=budget, trace=trace, replay=replay)


async def test_investigates_and_answers(fake):
    calls = fake("success.jsonl", tool='read_file:{"path": "retry.py"}')
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


async def test_max_turns_resumes_once_with_the_final_instruction(fake):
    calls = fake("max_turns.jsonl,success.jsonl")
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


async def test_usage_limit_is_overloaded(fake):
    fake("usage_limit.jsonl")
    provider = _provider()
    with pytest.raises(TypeSafeAPIError) as error:
        await provider.request(MESSAGES, schema=SCHEMA, structured=True)
    assert error.value.status == 529


async def test_auth_failure_is_an_engine_error(fake):
    fake("auth.jsonl")
    with pytest.raises(TypeSafeError):
        await _provider().request(MESSAGES, schema=SCHEMA, structured=True)


async def test_time_budget(fake):
    fake("hang")
    trace = Trace()
    with pytest.raises(AgentBudgetExceeded):
        await _provider(trace=trace, budget=1.0).request(MESSAGES, schema=SCHEMA, structured=True)
    assert trace.info["investigation"]["reason"] == "time_budget"


async def test_replay_serves_a_recorded_session(fake):
    calls = fake("success.jsonl")
    recorded = Trace()
    await _provider(trace=recorded).request(MESSAGES, schema=SCHEMA, structured=True)
    live_calls = len(calls())
    trace = Trace()
    result = await _provider(trace=trace, replay=Recordings([recorded.to_dict()])).request(MESSAGES, schema=SCHEMA, structured=True)
    assert json.loads(result.text) == {"answers": {"q": "yes"}}
    assert len(calls()) == live_calls  # no new CLI run
    assert trace.info["sessions"][0]["replayed"] is True


async def test_correction_is_a_fresh_session(fake):
    calls = fake("success.jsonl")
    provider = _provider()
    first = await provider.request(MESSAGES, schema=SCHEMA, structured=True)
    corrected = [*MESSAGES, Message(role="assistant", content=first.text), Message(role="user", content="fix the answer")]
    await provider.request(corrected, schema=SCHEMA, structured=True)
    second = calls()[-1]
    assert "--resume" not in second["argv"] and "--mcp-config" not in second["argv"]
    assert "Your previous answer" in second["stdin"] and "fix the answer" in second["stdin"]
```

In `tests/unit/test_evaluator.py`, add:

```python
def test_claude_code_needs_no_http_clients():
    from pacds.config import LLMConfig
    from pacds.engine.evaluator import Evaluator

    evaluator = Evaluator(LLMConfig.model_validate({"model": "haiku", "api": "claude_code"}))
    assert evaluator._client is None and evaluator._anthropic is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_claude_code_provider.py tests/unit/test_evaluator.py -q`
Expected: FAIL (`pacds.engine.claude_code_provider` missing; `Evaluator` builds an OpenAI client).

- [ ] **Step 3: Implement `src/pacds/engine/claude_code_provider.py`**

```python
"""The adapter provider for llm.api claude_code: one Claude Code session investigates and answers (pacds.engine.claude_code).

Same seam as AgentProvider: TypeSafe's adapter asks for answers to messages in a schema; here Claude Code runs the tool
loop over WorkspaceTools (served over MCP) and returns the answer as its structured output.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx2
from system_one_adapter.providers.base import Message, ProviderResult, render_messages
from typesafe_sdk import TypeSafeAPIError, TypeSafeError

from pacds.context import request_id
from pacds.engine import claude_code
from pacds.engine.agent_provider import AGENT_SYSTEM_PROMPT, FINAL_INSTRUCTION, AgentBudgetExceeded
from pacds.engine.claude_code import ClaudeCodeError, Result
from pacds.engine.mcp_http import Toolset
from pacds.engine.questions import describe_questions
from pacds.engine.replay import Recordings
from pacds.engine.trace import Trace, sha256

logger = logging.getLogger(__name__)


class ClaudeCodeProvider:
    def __init__(self, *, model_name: str, tools: Any, max_turns: int, time_budget_seconds: float, effort: str | None = None,
                 trace: Trace | None = None, replay: Recordings | None = None, system_prompt: str = AGENT_SYSTEM_PROMPT) -> None:
        self.model_name = model_name
        self._tools = tools
        self._max_turns = max_turns
        self._time_budget = time_budget_seconds
        self._effort = effort
        self._trace = trace
        self._replay = replay
        self._system = system_prompt
        self._prompt: str | None = None
        self._answer: str | None = None
        self._base_message_count = 0

    def translate_error(self, error: Exception) -> TypeSafeError:
        if isinstance(error, TypeSafeError):
            return error
        if isinstance(error, ClaudeCodeError) and error.kind == "usage_limit":
            return TypeSafeAPIError(529, None, httpx2.Headers(), message=str(error))
        return TypeSafeError(str(error))

    async def request(self, messages: list[Message], *, schema: dict[str, Any], structured: bool) -> ProviderResult:
        # Only ClaudeCodeError is translated: AgentBudgetExceeded must reach the Evaluator as it is (504).
        try:
            if self._prompt is None:
                document = [m["content"] for m in render_messages(messages) if m["role"] != "system"]
                self._prompt = "\n\n".join([describe_questions(schema), *document])
                self._base_message_count = len(messages)
                results = await self._investigate(schema)
            else:
                corrections = [m["content"] for m in render_messages(messages[self._base_message_count:]) if m["role"] != "assistant"]
                prompt = "\n\n".join([self._prompt, f"Your previous answer:\n{self._answer}", *corrections])
                results = [await self._session("correction", prompt, schema, tools=None, max_turns=2)]
        except ClaudeCodeError as error:
            raise self.translate_error(error) from error
        answer = results[-1].structured_output
        if answer is None:
            raise TypeSafeError(f"claude code session ended without an answer: {results[-1].subtype}")
        self._answer = json.dumps(answer)
        if self._trace is not None:
            self._trace.info["final_raw"] = self._answer
        return ProviderResult(text=self._answer, input_tokens=sum(r.usage.get("input", 0) for r in results),
                              output_tokens=sum(r.usage.get("output", 0) for r in results))

    async def _investigate(self, schema: dict[str, Any]) -> list[Result]:
        assert self._prompt is not None
        budget = asyncio.timeout(self._time_budget)
        results: list[Result] = []
        try:
            async with budget:
                first = await self._session("investigate", self._prompt, schema, tools=self._tools, max_turns=self._max_turns + 1, persist=True)
                results.append(first)
                if first.subtype == "error_max_turns":
                    results.append(await self._session("resume", FINAL_INSTRUCTION, schema, tools=self._tools, max_turns=2, persist=True,
                                                       resume=first))
        except TimeoutError:
            if budget.expired():
                self._ended(sum(r.num_turns for r in results), "time_budget")
                raise AgentBudgetExceeded("time budget exhausted") from None
            raise
        finally:
            for result in results:
                claude_code.forget_session(result)
        self._ended(sum(r.num_turns for r in results), "answered" if len(results) == 1 else "turn_budget_resumed")
        return results

    async def _session(self, label: str, prompt: str, schema: dict[str, Any], *, tools: Any, max_turns: int, persist: bool = False,
                       resume: Result | None = None) -> Result:
        definitions = tools.definitions if tools is not None else []
        request = {"api": "claude_code", "model": self.model_name, "effort": self._effort, "system": self._system, "prompt": prompt,
                   "schema": schema, "tools": definitions, "max_turns": max_turns,
                   "workspace": await tools.fingerprint() if tools is not None else None, "resume": resume is not None}
        digest = sha256(request)
        recorded = self._replay.take_session(digest) if self._replay is not None and resume is None else None
        if recorded is not None:
            if self._trace is not None:
                self._trace.add_session(recorded, request_sha256=digest, label=label, replayed=True, tools_from_result=True)
            return recorded
        toolset = Toolset(definitions, self._traced_call) if tools is not None else None
        result = await claude_code.run(system=self._system, prompt=prompt, schema=schema, model=self.model_name, max_turns=max_turns,
                                       toolset=toolset, effort=self._effort, persist=persist,
                                       resume=resume.session_id if resume else None, cwd=resume.cwd if resume else None)
        if self._trace is not None:
            self._trace.add_session(result, request_sha256=digest, label=label)
        return result

    async def _traced_call(self, name: str, arguments: dict[str, Any]) -> str:
        started = time.monotonic()
        result = await self._tools.call(name, arguments)
        if self._trace is not None:
            self._trace.add_tool(name, json.dumps(arguments), result, started)
        # Arguments and result size only: results are source code and logs.
        logger.info("agent tool request=%s tool=%s args=%s result_chars=%d", request_id.get(), name, json.dumps(arguments)[:300], len(result))
        return result

    def _ended(self, turns: int, reason: str) -> None:
        logger.info("investigation ended request=%s turns=%d reason=%s", request_id.get(), turns, reason)
        if self._trace is not None:
            self._trace.info["investigation"] = {"turns": turns, "reason": reason}
```

Note: a resumed session is never taken from replay (`resume is None` guard) — the whole investigation is replayed by
the first session's hash when it was recorded with a structured output; a recorded first session that hit max turns
has no structured output, is not replayable (Task 4), and so runs live with its resume.

`src/pacds/engine/evaluator.py`:
- `__init__`: `self._client = None if llm.api == "claude_code" else (client or openai.AsyncOpenAI(...))`, and the
  Anthropic client stays `None` for it (its condition is `llm.api == "anthropic"` already).
- `evaluate`: before building `AgentProvider`, branch:

```python
        if self._llm.api == "claude_code":
            provider: Any = ClaudeCodeProvider(model_name=self._llm.model, tools=tools, max_turns=self._llm.max_turns,
                                               time_budget_seconds=self._llm.time_budget_seconds, effort=self._llm.effort,
                                               trace=trace, replay=self._replay)
        else:
            (the existing session-header lines and AgentProvider(...) construction, unchanged)
```

  and import `ClaudeCodeProvider`. Keep `client, anthropic_client = ...` inside the `else`.

- [ ] **Step 4: Run the unit suite**

Run: `uv run pytest tests/unit -q; echo exit=$?`
Expected: `exit=0`.

- [ ] **Step 5: Contract check with the fake CLI**

Find the contract tests' way of starting PACDS (`ls tests/contract`, read its conftest). Add one test that configures
`llm: {model: haiku, api: claude_code}` with `PACDS_CLAUDE_EXECUTABLE` pointing at the fake and
`FAKE_CLAUDE_STREAM=success.jsonl`, sends a request whose question id is `q` with the official `typesafe-sdk`, and
asserts a typed answer comes back. If the contract harness cannot inject an env var into the server process, run the
server in-process as its fixtures do and set the env var with `monkeypatch` before startup. Run:
`uv run pytest tests/contract -q; echo exit=$?` → `exit=0`.

- [ ] **Step 6: Commit**

```bash
git add src/pacds/engine/claude_code_provider.py src/pacds/engine/evaluator.py tests/unit/test_claude_code_provider.py tests/unit/test_evaluator.py tests/contract
git commit -m "feat: investigate with Claude Code when llm.api is claude_code"
```

---

### Task 6: `check_llm` for Claude Code

**Files:**
- Modify: `src/pacds/devtools/check_llm.py`
- Test: `tests/unit/test_check_llm.py`

**Interfaces:**
- Consumes: `claude_code.run`, `Toolset`, `ClaudeCodeError`.
- Produces: `async run_claude_code(config) -> list[tuple[str, bool, str]]` with checks `session` (a plain schema answer:
  `{"word": "ready"}`) and `tools` (the `lookup` tool over MCP returns `blue`; the schema answer uses it). `main()` calls
  it when `config.llm.api == "claude_code"` and prints `LLM <model> via Claude Code (claude_code)`.

- [ ] **Step 1: Write the failing test** (append to `tests/unit/test_check_llm.py`)

```python
async def test_claude_code_checks_with_the_fake(tmp_path, monkeypatch):
    import sys
    from pathlib import Path

    from pacds.devtools import check_llm
    from pacds.engine import claude_code

    fake = Path(__file__).parent / "fake_claude.py"
    monkeypatch.setenv(claude_code.EXECUTABLE_ENV, f"{sys.executable} {fake}")
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "log"))
    monkeypatch.setenv("FAKE_CLAUDE_STREAM", "check_session.jsonl,check_tools.jsonl")
    monkeypatch.setenv("FAKE_CLAUDE_CALL_TOOL", 'lookup:{"key": "sky"}')

    class Llm:
        model, effort, api = "haiku", None, "claude_code"

    class Config:
        llm = Llm()
    results = await check_llm.run_claude_code(Config())
    assert [(name, ok) for name, ok, _ in results] == [("session", True), ("tools", True)]
```

Add `tests/unit/claude_streams/check_session.jsonl` (a `success` result with `structured_output: {"word": "ready"}`)
and `check_tools.jsonl` (an assistant `tool_use` id `t1` name `mcp__pacds__lookup`, a `tool_result` for `t1`, and a
`success` result with `structured_output: {"color": "blue"}`), in the same event shapes as `success.jsonl`. The fake
calls the tool only in the invocation that has `--mcp-config` (the tools check).

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_check_llm.py -q -k claude_code` → FAIL (`run_claude_code` missing).

- [ ] **Step 3: Implement** (in `check_llm.py`; update the module docstring's check list with the two `claude_code` checks)

```python
ADVICE["session"] = "install the Claude Code CLI and log in (claude, or CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`)"
ADVICE["claude_tools"] = "the session could not use a tool served over MCP: report the claude version and this output"


async def run_claude_code(config: Any) -> list[tuple[str, bool, str]]:
    from pacds.engine import claude_code
    from pacds.engine.mcp_http import Toolset

    llm, results = config.llm, []

    async def session() -> str:
        result = await claude_code.run(system="Answer with the requested JSON.", prompt="Reply with the word ready.",
                                       schema={"type": "object", "properties": {"word": {"type": "string"}}, "required": ["word"]},
                                       model=llm.model, max_turns=2, effort=llm.effort)
        if (result.structured_output or {}).get("word", "").lower() != "ready":
            raise AssertionError(f"unexpected answer {result.structured_output!r}")
        return f"answered in {result.latency_ms} ms ({result.turns[-1].model if result.turns else llm.model})"

    async def tools() -> str:
        seen = []

        async def lookup(name: str, arguments: dict[str, Any]) -> str:
            seen.append(arguments)
            return "blue"
        result = await claude_code.run(system="Use the lookup tool to answer.", prompt="What is the value of the key 'sky'? Look it up.",
                                       schema=SCHEMA, model=llm.model, max_turns=4, effort=llm.effort, toolset=Toolset([TOOL], lookup))
        if not seen:
            raise AssertionError("the lookup tool was not called")
        if (result.structured_output or {}).get("color") != "blue":
            raise AssertionError(f"the answer does not use the tool result: {result.structured_output!r}")
        return f"tool round trip and schema answer in {result.latency_ms} ms"

    for name, advice, check in (("session", "session", session), ("tools", "claude_tools", tools)):
        try:
            results.append((name, True, await check()))
        except Exception as error:  # noqa: BLE001 - report every check
            results.append((name, False, f"{type(error).__name__}: {str(error)[:200]} -> {ADVICE[advice]}"))
    return results
```

In `main()`: after `config = load_config(...)`:

```python
    if config.llm.api == "claude_code":
        print(f"LLM {config.llm.model} via Claude Code (claude_code)")
        results = asyncio.run(run_claude_code(config))
    else:
        print(f"LLM {config.llm.model} at {config.llm.base_url} ({config.llm.api})")
        results = asyncio.run(run(config, args.context_tokens))
```

- [ ] **Step 4: Run tests, then the live check with haiku**

Run: `uv run pytest tests/unit -q; echo exit=$?` → `exit=0`.
Live (host login): write `$SCRATCH/cc.yaml` as a copy of `dev/pacds.yaml` with `llm: {model: haiku, api: claude_code}`
and `trace` removed, then `uv run python -m pacds.devtools.check_llm $SCRATCH/cc.yaml; echo exit=$?`.
Expected: `ok session`, `ok tools`, `exit=0`. (`$SCRATCH` = the session scratchpad directory.)

- [ ] **Step 5: Commit**

```bash
git add src/pacds/devtools/check_llm.py tests/unit/test_check_llm.py tests/unit/claude_streams
git commit -m "feat: preflight the Claude Code backend"
```

---

### Task 7: Baseline, classifier and casebook on Claude Code

**Files:**
- Modify: `tests/replay/baseline.py`, `tests/analysis/classify.py`, `tests/replay/casebook.py`
- Test: `tests/unit/test_replay_baseline.py`, `tests/unit/test_analysis.py` (or where `classify` is tested), `tests/unit/test_casebook.py`

**Interfaces:**
- Consumes: `ClaudeCodeProvider` (Task 5), `claude_code.ask_json` (Task 3).
- Produces: each of the three works with `LLM_API=claude_code` and no `LLM_BASE_URL`.

Changes:
- `evaluate_baseline(...)`: when `api == "claude_code"`, build
  `ClaudeCodeProvider(model_name=model, tools=None, max_turns=1, time_budget_seconds=timeout_seconds, effort=os.environ.get("LLM_EFFORT") or None,
  trace=trace, replay=replay, system_prompt=BASELINE_SYSTEM_PROMPT)` instead of `BaselineProvider`; the adapter call is
  unchanged. (`max_turns=1` + the `StructuredOutput` turn = `--max-turns 2`.)
- `harness._run_baseline`: create the OpenAI client only when `LLM_API != "claude_code"` (pass `client=None` otherwise).
- `classify._ask(client, model, api, text)`: when `api == "claude_code"`, `answer, usage = await claude_code.ask_json(system=prompt(), prompt=text, schema=SCHEMA, model=model)`
  and return `(answer, {"input": usage["input"], "output": usage["output"]})`. In `classify()`, build the OpenAI client
  only when `api != "claude_code"`.
- `casebook.review_with_llm`: when `api == "claude_code"`, `answer, _ = asyncio.run(claude_code.ask_json(system=REVIEW_PROMPT.read_text(), prompt=packet(cases_dir, case_id), schema=REVIEW_SCHEMA, model=model))`;
  do not build the OpenAI client for it.

- [ ] **Step 1: Write the failing tests** — one per client, all with the fake CLI (same `fake` fixture as Task 3;
  move that fixture into `tests/unit/conftest.py` as `fake_claude` and use it from Tasks 3, 5, 6 and here — update
  those test files in this step so there is one fixture).

For the baseline (in `tests/unit/test_replay_baseline.py`): a stream `baseline.jsonl` whose `structured_output` is the
adapter's answer shape for the `cause` Choice question. Build it by reading the schema the adapter sends: first run
the test with a stream whose output is `{}` and print the `--json-schema` argument from the fake's log, then write the
stream to match it (`{"answers": {"cause": {<option>: <probability>, ...}}}` in probabilities mode). Assert
`result.predicted == <the option with the highest probability>` and that no OpenAI client was needed
(`client=None`, `api="claude_code"`).

For the classifier: monkeypatch `claude_code.ask_json` to return `({"mode": "<one of LLM_MODES>", ...required fields of SCHEMA}, {"input": 3, "output": 2})`,
call `classify._ask(None, "haiku", "claude_code", "dossier")`, assert the answer and usage pass through.

For the casebook: monkeypatch `claude_code.ask_json` the same way with a `REVIEW_SCHEMA`-shaped answer, set
`LLM_API=claude_code`, `LLM_MODEL=haiku`, unset `LLM_BASE_URL`, run `review_with_llm` on a one-case temporary case set
built as the existing casebook tests build theirs, assert two reviews were added (`llm-1:haiku`, `llm-2:haiku`).

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/unit -q -k "claude_code"` → FAIL.

- [ ] **Step 3: Implement** the changes listed above.

- [ ] **Step 4: Run the unit suite** — `uv run pytest tests/unit -q; echo exit=$?` → `exit=0`.

- [ ] **Step 5: Commit**

```bash
git add tests/replay/baseline.py tests/replay/harness.py tests/analysis/classify.py tests/replay/casebook.py tests/unit
git commit -m "feat: run the baseline, classifier and case reviews on Claude Code"
```

---

### Task 8: Support agent on Claude Code

**Files:**
- Modify: `tests/support_agent/agent.py`, `tests/support_agent/run.py`
- Test: `tests/unit/test_support_agent.py`

**Interfaces:**
- Consumes: `claude_code.run`, `Toolset`, `Trace.add_session`, `Recordings.take_session`.
- Produces: `run_agent(..., api="claude_code")` returns the same `Outcome` fields as the chat path.

Behavior (`_converse_claude_code`, called from `run_agent` when `api == "claude_code"`):
- `system = _system_prompt(case, with_pacds) + "\n\n" + DECISION_NOTE` where
  `DECISION_NOTE = "Submit your decision as your final structured output (class, escalate, confidence): that is your submit_decision."`
- `prompt = ticket_message(case, log_texts)`; `schema = SUBMIT_DECISION_TOOL["function"]["parameters"]`.
- Toolset: `CALL_PACDS_TOOL` only when `pacds` is given; its `call` runs the existing `_tool_result(...)` and
  `trace.add_tool(...)` with the linked `pacds_request_id` exactly as `_converse` does.
- `max_turns + 1` for the `StructuredOutput` turn; no persistence.
- Replay: only when `pacds is None` (spec §4). Hash `{"api": "claude_code", "model", "system", "prompt", "schema", "tools", "max_turns"}`.
- Outcome: `decision/escalate/confidence` from the structured output (`class` must be in `CLASSES`, else `decision=None`
  and `error="no valid decision"`); `turns = result.num_turns`; `input_tokens = result.usage["input"]`; `transcript` =
  `[{"role": "user", "content": prompt}]` then per turn an assistant message (`content`, `tool_calls` in function form
  `{"id", "type": "function", "function": {"name", "arguments"}}` when any) followed by one `{"role": "tool", ...}` per
  tool call from `result.tool_results`. `error_max_turns` → `error="turn budget exhausted"`. `ClaudeCodeError` propagates
  to `run_agent`'s existing `except` (it becomes `outcome.error`).
- `run.py`: build the OpenAI client only when `LLM_API != "claude_code"`; pass `effort` from `LLM_EFFORT`.

- [ ] **Step 1: Write the failing tests** (in `tests/unit/test_support_agent.py`, using the `fake_claude` fixture)

A stream `support.jsonl`: assistant `tool_use` `t1` `mcp__pacds__call_pacds` with input
`{"document": {"user_report": "r"}, "logs": [], "questions": {"q": {"kind": "noul"}}}`, the `tool_result` for `t1`,
assistant `StructuredOutput` `{"class": "B", "escalate": true, "confidence": 0.8}`, and a `success` result carrying it.
With `FAKE_CLAUDE_CALL_TOOL='call_pacds:{"document": {"user_report": "r"}, "logs": [], "questions": {"q": {"kind": "noul"}}}'`
and a stub `pacds` caller returning `{"answers": {"q": "yes"}, "request_id": "req-1"}`:

```python
async def test_claude_code_ticket(fake_claude, tmp_path):
    fake_claude("support.jsonl", tool='call_pacds:{"document": {"user_report": "r"}, "logs": [], "questions": {"q": {"kind": "noul"}}}')
    seen = []

    async def pacds(case, body):
        seen.append(body)
        return {"answers": {"q": "yes"}, "request_id": "req-1"}
    outcome = await run_agent(CASE, log_texts={}, client=None, model="haiku", pacds=pacds, api="claude_code")
    assert (outcome.decision, outcome.escalate, outcome.confidence) == ("B", True, 0.8)
    assert outcome.pacds_requests[0]["request_id"] == "req-1" and seen[0]["state"]["pacds"]["git"]["url"] == CASE.repo
    assert outcome.transcript[1]["tool_calls"][0]["function"]["name"] == "call_pacds"
    assert outcome.transcript[2]["role"] == "tool"
```

(`CASE` = the `Case` the module's existing tests use.) Add a second test: with `pacds=None` and a recorded trace, the
second run is replayed (fake log line count unchanged); and a third: with `pacds` set and a recorded trace, it runs
live (line count grows).

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/unit/test_support_agent.py -q -k claude_code` → FAIL.

- [ ] **Step 3: Implement** as described.

- [ ] **Step 4: Run the unit suite** — `uv run pytest tests/unit -q; echo exit=$?` → `exit=0`.

- [ ] **Step 5: Commit**

```bash
git add tests/support_agent tests/unit
git commit -m "feat: run the support agent as a Claude Code session"
```

---

### Task 9: Image, Compose, eval.sh and docs

**Files:**
- Modify: `Dockerfile`, `compose.yaml`, `scripts/eval.sh`, `.env.example`, `docs/evaluation-runbook.md`, `README.md`
- Test: `tests/unit/test_deployment.py` (or the test that already checks `compose.yaml` / `Dockerfile`)

**Interfaces:**
- Produces: `CLAUDE_CODE_VERSION` build argument; `CLAUDE_CODE_OAUTH_TOKEN` passed to the `pacds` service;
  `eval.sh` guard.

- [ ] **Step 1: Dockerfile** — in the `runtime` stage, after the `apt-get` block and before `COPY --from=uv`:

```dockerfile
# Development and evaluation only (llm.api claude_code): the Claude Code CLI, which runs on a subscription's token
# (CLAUDE_CODE_OAUTH_TOKEN). Empty by default, so the production image does not contain it.
ARG CLAUDE_CODE_VERSION=
RUN if [ -n "$CLAUDE_CODE_VERSION" ]; then \
      apt-get update && apt-get install -y --no-install-recommends curl \
      && curl -fsSL https://claude.ai/install.sh | bash -s "$CLAUDE_CODE_VERSION" \
      && install -m 0755 "$(readlink -f /root/.local/bin/claude)" /usr/local/bin/claude \
      && rm -rf /root/.local /root/.claude /root/.claude.json \
      && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*; \
    fi
ENV DISABLE_AUTOUPDATER=1
```

Verify: `docker build --build-arg CLAUDE_CODE_VERSION=$(claude --version | awk '{print $1}') -t pacds-cc . && docker run --rm --entrypoint claude pacds-cc --version`
→ prints the version. If the installer's layout differs (no `/root/.local/bin/claude`), find the binary with
`docker run --rm ... sh -c 'ls -la /root/.local/bin /root/.local/share/claude'` in a debug build and fix the `install`
line; report the finding. Also `docker build -t pacds .` (no argument) must not contain `claude`:
`docker run --rm --entrypoint sh pacds -c 'command -v claude || echo absent'` → `absent`.

- [ ] **Step 2: compose.yaml** — under `services.pacds.build` add `args: {CLAUDE_CODE_VERSION: "${CLAUDE_CODE_VERSION:-}"}`;
  under `services.pacds.environment` add `CLAUDE_CODE_OAUTH_TOKEN: ${CLAUDE_CODE_OAUTH_TOKEN:-}`. The service is on
  the `default` network, which reaches the internet; nothing else changes. The container's `HOME=/tmp` is a tmpfs, where
  the CLI writes its state.

- [ ] **Step 3: eval.sh** — after the `NEEDS_LLM` check:

```bash
# LLM_API=claude_code: the clients run the host's claude; PACDS in the Compose stack needs the CLI in its image and a
# subscription token (claude setup-token).
if [ "${LLM_API:-}" = claude_code ]; then
  command -v claude >/dev/null || { echo "ERROR: LLM_API=claude_code needs the claude CLI on this host." >&2; exit 1; }
  if [ -z "$TARGET" ]; then
    if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
      echo "ERROR: LLM_API=claude_code needs CLAUDE_CODE_OAUTH_TOKEN for PACDS in the stack (run: claude setup-token)." >&2
      exit 1
    fi
    export CLAUDE_CODE_VERSION="${CLAUDE_CODE_VERSION:-$(claude --version | awk '{print $1}')}"
  fi
fi
```

Also make sure `eval_run record` never writes `CLAUDE_CODE_OAUTH_TOKEN` into `run.json` (it records `LLM_model`,
`LLM_base_url`, `LLM_api` only today — confirm by reading `tests/eval_run.py:record`; add a unit test that sets the
token and asserts it is absent from the recorded JSON).

- [ ] **Step 4: .env.example** — append:

```bash
# Claude subscription instead of an API (development and evaluation only):
#   LLM_API=claude_code
#   LLM_MODEL=haiku            # or sonnet, opus, a full model id
#   CLAUDE_CODE_OAUTH_TOKEN=   # from `claude setup-token`; PACDS in the Compose stack uses it
# LLM_BASE_URL and LLM_API_KEY are not used then.
```

- [ ] **Step 5: Docs** — `docs/evaluation-runbook.md`: a section "Claude Code backend (subscription)": what it is,
  `claude setup-token`, `.env` values, `python -m pacds.devtools.check_llm` with a `claude_code` config, that replay is
  per session and support-agent tickets with PACDS never replay, that `--concurrency` may need lowering for the
  subscription's rate limits, that a subscription must not back a shared deployment, and that `tests.analysis context`
  does not apply. `README.md`: one line in the LLM section pointing to it.

- [ ] **Step 6: Tests and commit**

Run: `uv run pytest tests/unit -q; echo exit=$?` → `exit=0`; `bash -n scripts/eval.sh; echo exit=$?` → `exit=0`.

```bash
git add Dockerfile compose.yaml scripts/eval.sh .env.example docs/evaluation-runbook.md README.md tests/unit tests/eval_run.py
git commit -m "build: Claude Code CLI in the dev image and eval.sh support"
```

---

### Task 10: Live verification with haiku

No code unless a step fails; findings go into `docs/evaluation/2026-09-30-claude-code-backend.md`.

- [ ] **Step 1: Token.** Ask the user to run `! claude setup-token` and put the token in `.env` as
  `CLAUDE_CODE_OAUTH_TOKEN` (never paste it into the conversation or a commit). Set `LLM_API=claude_code`, `LLM_MODEL=haiku`.
- [ ] **Step 2: Live unit checks.** `uv run pytest -m claude_code tests/live -q; echo exit=$?` → `exit=0`.
- [ ] **Step 3: Container preflight.** `docker compose up -d --build --wait`, then
  `docker compose exec pacds python -m pacds.devtools.check_llm; echo exit=$?` → `ok session`, `ok tools`, `exit=0`.
  `docker compose down -v`.
- [ ] **Step 4: Clear set, PACDS and baseline.**
  `./scripts/eval.sh --replay "--set clear" --replay "--baseline --set clear"; echo exit=$?`.
  Expect the run to complete with no infrastructure errors (`errors.json` empty). Record accuracy, tokens, cost
  (`sessions[].cost_usd` summed from `traces/pacds`), latency per investigation.
- [ ] **Step 5: Support agent.** `./scripts/eval.sh --support "--variant full --set clear" --support "--variant no-pacds --set clear"; echo exit=$?`.
- [ ] **Step 6: Replay.** Repeat Step 4 with `--replay-from <that run>`: every PACDS investigation and baseline answer
  must be replayed (no `claude` process for them: `sessions[].replayed` true), same results.
- [ ] **Step 7: Report.** Write the findings note (what ran, numbers, anything that failed and why), update the spec's
  status line to "Implemented (2026-09-30)", commit:

```bash
git add docs/evaluation/2026-09-30-claude-code-backend.md docs/superpowers/specs/2026-09-30-claude-code-backend-design.md
git commit -m "docs: first Claude Code backend runs"
```

The Debezium milestone at one repeat is a separate decision for the user (it uses real subscription quota even on
haiku); propose it with the clear-set numbers, do not start it.

---

## Self-Review Notes

- Spec §3 Configuration → Task 1; Runner → Task 3; MCP server → Task 2; ClaudeCodeProvider → Task 5; Clients → Tasks 7, 8;
  Images → Task 9. §4 Traces/Replay → Task 4 (+ provider and agent use in 5, 8). §5 Errors → Tasks 3, 5. §6 Testing →
  Tasks 3 (fake, live), 5 (contract), 6 (check_llm), 10 (live runs, haiku).
- `tests.analysis context` skips Claude Code traces without changes: their calls have phase `claude_code`, so
  `investigation()` finds no `investigate`/`final` calls and returns None (Task 4's phase choice). Mention in the runbook (Task 9).
