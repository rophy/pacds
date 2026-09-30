"""Preflight: does the configured LLM server support what a PACDS investigation needs? Run before any evaluation.

Usage: pacds check-llm [CONFIG] [--context-tokens N]
       (on a deployment: docker compose --env-file .env run --rm pacds check-llm)
Checks, with PACDS's own client settings (base_url, api, model, key, TLS, extra_body):
  chat         a plain answer
  tools        a tool call, then an answer after the tool result (vLLM: --enable-auto-tool-choice --tool-call-parser)
  json_schema  an answer constrained to a JSON schema, with the tools present and tool_choice none as PACDS's final
               request sends it (tools left out with llm.final_keeps_tools false; skipped when llm.structured_outputs is false)
  context      a request of about N tokens (default 64000, above the largest investigation request measured, 60K)
With llm.api claude_code (the Claude Code CLI), instead:
  session      a plain schema answer ({"word": "ready"})
  tools        the lookup tool served over MCP is called, and the schema answer uses its result
Exit status 1 when a check fails; each failure says what to change.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pacds import tls
from pacds.config import Config, load_config
from pacds.engine.agent_provider import AgentProvider
from pacds.engine.evaluator import Evaluator

TOOL = {"type": "function", "function": {"name": "lookup", "description": "Look up the value of a key.",
                                         "parameters": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"],
                                                        "additionalProperties": False}}}
SCHEMA = {"type": "object", "properties": {"color": {"type": "string", "enum": ["red", "green", "blue"]}, "confidence": {"type": "number"}},
          "required": ["color", "confidence"], "additionalProperties": False}
ADVICE = {
    "chat": "check llm.base_url, llm.model (vLLM --served-model-name), llm.api_key and TLS (tls.ca_file)",
    "tools": "start vLLM with --enable-auto-tool-choice and the model's --tool-call-parser; the model must support tool calling",
    "json_schema": "if the server rejects tool_choice none, set llm.final_keeps_tools: false; "
                   "if it rejects guided decoding, set llm.structured_outputs: false",
    "context": "raise vLLM --max-model-len (PACDS needs about 64K; 128K is comfortable) or use a longer-context model",
}
ADVICE["session"] = "install the Claude Code CLI and log in (claude, or CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`)"
ADVICE["claude_tools"] = "the session could not use a tool served over MCP: report the claude version and this output"


def provider(config: Config) -> AgentProvider:
    evaluator = Evaluator(config.llm, verify=tls.context(tls.ca_bundle(config.tls.ca_file, config.work_dir)))
    client, anthropic_client = evaluator._client, evaluator._anthropic
    if config.llm.session_header:  # as Evaluator.evaluate does per investigation
        headers = {config.llm.session_header: f"pacds-check-llm-{uuid.uuid4()}"}
        client = client.with_options(default_headers=headers)
        anthropic_client = anthropic_client.with_options(default_headers=headers) if anthropic_client else None
    return AgentProvider(model_name=config.llm.model, client=client, tools=None, max_turns=1,  # type: ignore[arg-type]
                         time_budget_seconds=config.llm.timeout_seconds, api=config.llm.api, max_output_tokens=config.llm.max_output_tokens,
                         anthropic_client=anthropic_client, effort=config.llm.effort, extra_body=config.llm.extra_body,
                         final_keeps_tools=config.llm.final_keeps_tools)


async def check_chat(llm: AgentProvider) -> str:
    response = await llm._complete(messages=[{"role": "user", "content": "Reply with the single word: ready"}])
    text = response.choices[0].message.content or ""
    if not text.strip():
        raise AssertionError("empty answer")
    return f"answered {text.strip()[:40]!r} ({response.model})"


async def check_tools(llm: AgentProvider) -> str:
    messages: list[dict[str, Any]] = [{"role": "system", "content": "Use the lookup tool to answer."},
                                      {"role": "user", "content": "What is the value of the key 'sky'? Look it up."}]
    first = await llm._complete(messages=messages, tools=[TOOL])
    calls = first.choices[0].message.tool_calls or []
    if not calls or calls[0].function.name != "lookup":
        raise AssertionError(f"no tool call (finish reason {first.choices[0].finish_reason}); text: {(first.choices[0].message.content or '')[:80]!r}")
    json.loads(calls[0].function.arguments or "{}")
    messages += [{"role": "assistant", "content": first.choices[0].message.content or "",
                  "tool_calls": [{"id": calls[0].id, "type": "function", "function": {"name": "lookup", "arguments": calls[0].function.arguments}}]},
                 {"role": "tool", "tool_call_id": calls[0].id, "content": "blue"}]
    second = await llm._complete(messages=messages, tools=[TOOL])
    if "blue" not in (second.choices[0].message.content or "").lower():
        raise AssertionError(f"the answer after the tool result does not use it: {(second.choices[0].message.content or '')[:80]!r}")
    return f"called lookup({calls[0].function.arguments}) and used the result"


async def check_json_schema(llm: AgentProvider) -> str:
    tools = {"tools": [TOOL], "tool_choice": "none"} if llm._final_keeps_tools else {}
    response = await llm._complete(
        messages=[{"role": "user", "content": "The sky is blue. Which color is the sky? Answer as JSON."}],
        response_format={"type": "json_schema", "json_schema": {"name": "check", "schema": SCHEMA, "strict": True}}, **tools)
    answer = json.loads(response.choices[0].message.content or "")
    if set(answer) != {"color", "confidence"} or answer["color"] not in ("red", "green", "blue"):
        raise AssertionError(f"answer does not match the schema: {answer}")
    return f"answered {answer}"


def check_context(tokens: int) -> Callable[[AgentProvider], Awaitable[str]]:
    async def check(llm: AgentProvider) -> str:
        # This 45-character sentence is about 10 tokens; the marker sits at the start, so the model must read it all.
        filler = "The quick brown fox jumps over the lazy dog. " * (tokens // 10)
        started = time.monotonic()
        response = await llm._complete(messages=[{"role": "user", "content": f"The code word is PELICAN.\n\n{filler}\n\nWhat is the code word? One word."}])
        used = response.usage.prompt_tokens if response.usage else 0
        if "pelican" not in (response.choices[0].message.content or "").lower():
            raise AssertionError(f"lost the start of a {used}-token request")
        return f"{used:,} tokens in {time.monotonic() - started:.1f}s"

    return check


async def run(config: Config, context_tokens: int) -> list[tuple[str, bool, str]]:
    llm = provider(config)
    checks: list[tuple[str, Callable[[AgentProvider], Awaitable[str]]]] = [("chat", check_chat), ("tools", check_tools)]
    if config.llm.structured_outputs:
        checks.append(("json_schema", check_json_schema))
    checks.append(("context", check_context(context_tokens)))
    results = []
    for name, check in checks:
        try:
            results.append((name, True, await check(llm)))
        except Exception as error:  # noqa: BLE001 - report every check
            results.append((name, False, f"{type(error).__name__}: {str(error)[:200]} -> {ADVICE[name]}"))
    return results


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("config", nargs="?", type=Path, default=Path(os.environ.get("PACDS_CONFIG", "/etc/pacds/config.yaml")))
    parser.add_argument("--context-tokens", type=int, default=64000)
    args = parser.parse_args()
    config = load_config(args.config)
    if config.llm.api == "claude_code":
        print(f"LLM {config.llm.model} via Claude Code (claude_code)")
        results = asyncio.run(run_claude_code(config))
    else:
        print(f"LLM {config.llm.model} at {config.llm.base_url} ({config.llm.api})")
        results = asyncio.run(run(config, args.context_tokens))
    for name, ok, detail in results:
        print(f"  {'ok  ' if ok else 'FAIL'} {name:12} {detail}")
    if not all(ok for _, ok, _ in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
