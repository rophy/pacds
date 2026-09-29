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
