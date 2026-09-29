# Claude Code backend — Design

**Date:** 2026-09-30
**Status:** Approved design, not implemented
**Context:** every LLM path (PACDS investigations, support agent, baseline, classifier, casebook reviews) needs an API
key and an OpenAI-compatible or Anthropic endpoint. A Claude subscription gives no API key, but it runs the Claude Code
CLI (`claude -p`). This adds `claude_code` as a backend so a whole `eval.sh` run needs only the subscription.

Scope: development and evaluation. A subscription is for its owner's own use; a PACDS serving other people (the
corporate deployment) keeps an API backend.

## 1. Where it plugs in

```
Jev API (/v1/systemone)
  → TypeSafe's system-one-adapter: prompt, typed-answer validation, malformed-answer retries
    → provider.request(messages, schema, structured) -> text        ← the seam
      → AgentProvider (today): our tool loop over WorkspaceTools, then the final JSON
      → ClaudeCodeProvider (new): one `claude -p` session with WorkspaceTools over MCP and --json-schema
```

Claude Code is itself an agent with a tool loop, so it replaces our loop for a whole investigation rather than
emulating single model calls. The Jev API, adapter, validation, auth, authorization and audit are unchanged.

## 2. Feasibility (probe, 2026-09-30, claude 2.1.284)

`claude -p` with `--tools ""`, `--strict-mcp-config --mcp-config <one stdio server>`, `--allowedTools mcp__ws__read_file`,
`--setting-sources ""`, `--no-session-persistence`, `--system-prompt`, `--json-schema` and `--output-format stream-json
--verbose` on subscription auth (`apiKeySource: none`): the session saw only `StructuredOutput` and the MCP tool, called
the tool, and returned the schema's object as `structured_output` (2.6 s, 3 turns). The stream carries each tool call and
result and per-turn usage including cache reads. `--bare` is not usable: it reads only `ANTHROPIC_API_KEY`.

## 3. Components

**Configuration.** `llm.api: claude_code` (PACDS) and `LLM_API=claude_code` (clients). `model` is a Claude Code model
name (`sonnet`, `opus` or a full model id); `base_url` and `api_key` are not required for this api. `effort` maps to
`--effort`. Authentication: `CLAUDE_CODE_OAUTH_TOKEN`, made once on the host with `claude setup-token`; the process
environment passes it through, never logged or traced.

**Runner — `src/pacds/engine/claude_code.py`.** `run(system, prompt, schema, tools, *, model, effort, max_turns,
resume=None) -> Result`:

- Command: `claude -p <prompt> --system-prompt <system> --model <model> --tools "" --strict-mcp-config --mcp-config
  <json> --allowedTools <mcp tool names> --setting-sources "" --no-session-persistence --json-schema <schema>
  --output-format stream-json --verbose --max-turns <n>`, stdin `/dev/null`, working directory an empty temporary
  directory, `HOME` a per-process temporary directory holding nothing but what the CLI writes.
  `--no-session-persistence` is dropped when the caller may resume (PACDS corrections, max-turns recovery).
- Parses the event stream into `Result`: session id, `subtype`, `is_error`, `structured_output`, assistant turns
  (text, tool calls, per-turn usage), total usage, `total_cost_usd`, `num_turns`, stderr tail.
- The caller owns the timeout: cancelling the task kills the process group.

**MCP server — `src/pacds/engine/mcp_http.py`.** A minimal MCP server over streamable HTTP on `127.0.0.1` (random
port), started once per process and shared: `initialize`, `tools/list`, `tools/call`, JSON responses, no sessions or
notifications. Each `claude` run registers a toolset under a random path token (`/mcp/<token>`) and gets
`{"mcpServers": {"pacds": {"type": "http", "url": ".../mcp/<token>"}}}`; the token is removed when the run ends. A
toolset is `definitions` plus an async `call(name, arguments) -> str`, so tool calls run in-process: PACDS gets
`WorkspaceTools` (path confinement unchanged), the support agent gets `call_pacds` (call limit, outcome records and
attachment URLs unchanged). Written against the MCP spec rather than the `mcp` SDK (2.x just renamed its server API);
the live check confirms Claude Code accepts it.

**PACDS — `ClaudeCodeProvider`** (`src/pacds/engine/claude_code_provider.py`), chosen by `Evaluator` when
`llm.api == "claude_code"`:

- First `request`: `run(AGENT_SYSTEM_PROMPT, questions + document, schema, WorkspaceTools, max_turns=llm.max_turns)`
  inside the existing `asyncio.timeout(time_budget_seconds)`. `ready_to_answer` is not offered: `StructuredOutput`
  ends the session.
- `error_max_turns`: resume the session once with `FINAL_INSTRUCTION` and `--max-turns 2`, as our loop forces a final
  answer today.
- Adapter corrections (malformed answer): resume the session with the correction messages instead of re-investigating.
- `structured=False` (schema in the prompt) is not needed: `--json-schema` is always used; the setting is ignored.
- Returns `json.dumps(structured_output)` as `ProviderResult.text` with the session's token counts.

**Clients.** A shared client helper (`tests/claude_code_client.py`, a thin layer over the runner):

- Support agent: `run_agent` with `api == "claude_code"` runs one session: system prompt and ticket as today,
  `call_pacds` over MCP, and `submit_decision`'s parameters as the `--json-schema`; `max_turns` and `max_pacds_calls`
  as today. The transcript is rebuilt from the stream.
- Baseline (`tests/replay/harness.py`), classifier (`tests/analysis/classify.py`), casebook reviews
  (`tests/replay/casebook.py`): one call each with no tools and `--max-turns 2`, the schema they already use as
  `--json-schema`.

**Images.** `Dockerfile` build argument `CLAUDE_CODE_VERSION` (empty by default): when set, the image installs that
CLI version with the native installer. The production image is unchanged. `compose.yaml` builds the dev image with
it when `LLM_API=claude_code` and passes `CLAUDE_CODE_OAUTH_TOKEN` to the PACDS and test-runner services; `eval.sh`
refuses to start with `LLM_API=claude_code` and no token.

## 4. Traces and replay

**Traces** keep the current file format, filled from the stream:

- `calls`: one per assistant turn — text, tool calls, per-turn usage (input, output, cache read and write). `usage`
  totals and `tests.analysis report` work unchanged.
- `tools`: recorded by the MCP handler (arguments, result, latency, error), not from the stream.
- `config.api = "claude_code"`, plus `session_id`, `num_turns` and `total_cost_usd`.
- `tests.analysis context` skips `claude_code` traces with a note: Claude Code manages its own context.

**Replay** is per session, not per call. The request hash covers model, effort, system prompt, prompt, schema, tool
definitions, `max_turns` and, for PACDS, the checkout's commit. A recorded session with that hash returns its
structured output and copies its trace (marked replayed); anything changed runs the whole session live. Support-agent
tickets hash their ticket and prompt; their PACDS calls are live or replayed by PACDS itself. Infrastructure failures
are never replayed, as today.

## 5. Errors and limits

| Claude Code outcome | Result |
|---|---|
| `success` with `structured_output` | answers to the adapter, which validates them |
| `error_max_turns` | one resume with `FINAL_INSTRUCTION`, `--max-turns 2`; then as below |
| no `structured_output` after that | `TypeSafeError` → 500 `engine_error` (the adapter's retries apply first) |
| time budget expires | process killed → 504 `agent_budget_exceeded` |
| usage or rate limit (`is_error` with a limit message) | 529 `overloaded`; the reset time is logged |
| not authenticated, CLI missing, non-zero exit | 500 `engine_error`; stderr tail logged, never returned |

Concurrency: one `claude` process (about 200 MB) per investigation or ticket; the subscription's rate limits bind first.
The runbook notes lowering `--concurrency` for this backend.

## 6. Testing

- Unit, no CLI: a fake `claude` executable replaying canned stream-json (success, max turns then resume, correction
  resume, usage limit, auth failure, hang for timeout); command-line isolation flags; config without `base_url` /
  `api_key`; trace building; replay hashing and hits; the MCP server (`initialize`, `tools/list`, `tools/call`,
  unknown token, path confinement through `WorkspaceTools`).
- Contract: the official `typesafe-sdk` against PACDS with the fake CLI returns typed answers.
- Live (subscription): `python -m pacds.devtools.check_llm` gains a `claude_code` mode (MCP tool round trip and a
  schema answer). Then `eval.sh --replay "--set clear"` once, then the Debezium milestone at one repeat, compared with
  the gpt-6-luna milestone (`docs/evaluation/2026-09-28-findings.md` §5).

## 7. Out of scope

Per-call replay and `llm.context_budget_tokens` for this backend; using a subscription for a shared deployment;
Claude Code's own built-in tools (Read, Grep, Bash) in place of `WorkspaceTools`.
