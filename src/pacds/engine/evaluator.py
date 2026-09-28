"""Run TypeSafe's adapter with our AgentProvider and map failures to PACDS errors."""

from __future__ import annotations

import logging
import ssl
import uuid
from dataclasses import dataclass
from typing import Any

import anthropic
import openai
from system_one_adapter import AsyncSystemOneAdapterClient
from system_one_adapter._schema import Question
from typesafe_sdk import (
    Answer,
    RetryPolicy,
    TypeSafeAPIConnectionError,
    TypeSafeAPIError,
    TypeSafeAPIResponseValidationError,
    TypeSafeError,
)
from typesafe_sdk._core.errors import parse_retry_after

from pacds.config import LLMConfig
from pacds.engine.agent_provider import AGENT_SYSTEM_PROMPT, FINAL_INSTRUCTION, READY_TOOL, AgentBudgetExceeded, AgentProvider
from pacds.engine.tools import TOOL_DEFINITIONS, WorkspaceTools
from pacds.engine.replay import Recordings
from pacds.engine.trace import Trace, sha256
from pacds.errors import PacdsError

logger = logging.getLogger(__name__)

OVERLOADED_STATUSES = frozenset({429, 503, 529})
# Retry-After beyond this means a quota reset, not a blip: fail fast instead of sleeping.
MAX_RETRY_AFTER_SECONDS = 10


def _worth_retrying(error: BaseException) -> bool:
    if not isinstance(error, TypeSafeAPIError) or error.status not in OVERLOADED_STATUSES:
        return False
    delay_ms = parse_retry_after(error.headers)
    return delay_ms is None or delay_ms <= MAX_RETRY_AFTER_SECONDS * 1000


@dataclass(frozen=True)
class Evaluation:
    answers: dict[str, Answer]
    input_tokens: int
    output_tokens: int


class Evaluator:
    def __init__(self, llm: LLMConfig, *, client: openai.AsyncOpenAI | None = None, replay: Recordings | None = None,
                 verify: ssl.SSLContext | bool = True) -> None:
        self._llm = llm
        self._replay = replay
        self._client = client or openai.AsyncOpenAI(
            base_url=llm.base_url, api_key=llm.api_key, max_retries=0, timeout=llm.timeout_seconds,
            http_client=openai.DefaultAsyncHttpxClient(verify=verify) if verify is not True else None)
        # Anthropic's SDK appends /v1/messages itself: base_url is the API root (e.g. https://opencode.ai/zen).
        self._anthropic = (anthropic.AsyncAnthropic(
            base_url=llm.base_url.removesuffix("/v1"), api_key=llm.api_key, max_retries=0, timeout=llm.timeout_seconds,
            http_client=anthropic.DefaultAsyncHttpxClient(verify=verify) if verify is not True else None)
            if llm.api == "anthropic" else None)
        self._adapter = AsyncSystemOneAdapterClient(
            structured_outputs=llm.structured_outputs,
            llm_answer_mode="probabilities",
            normalize_probabilities=True,
            n_retry_malformed_structure=2,
            retry=RetryPolicy(max_retries=1, timeout=None, http_statuses=set(), predicate=_worth_retrying),
        )

    async def evaluate(self, state: dict[str, Any], questions: dict[str, Question], tools: WorkspaceTools, trace: Trace | None = None) -> Evaluation:
        if trace is not None:
            llm = self._llm
            trace.info["config"] = {"model": llm.model, "api": llm.api, "effort": llm.effort, "max_turns": llm.max_turns,
                                    "time_budget_seconds": llm.time_budget_seconds, "max_output_tokens": llm.max_output_tokens}
            trace.info["prompt_sha256"] = {"system": sha256(AGENT_SYSTEM_PROMPT), "final": sha256(FINAL_INSTRUCTION),
                                           "tools": sha256([*TOOL_DEFINITIONS, READY_TOOL])}
        client, anthropic_client = self._client, self._anthropic
        if self._llm.session_header:
            headers = {self._llm.session_header: str(uuid.uuid4())}
            client = client.with_options(default_headers=headers)
            anthropic_client = anthropic_client.with_options(default_headers=headers) if anthropic_client else None
        provider = AgentProvider(
            model_name=self._llm.model,
            client=client,
            tools=tools,
            max_turns=self._llm.max_turns,
            time_budget_seconds=self._llm.time_budget_seconds,
            api=self._llm.api,
            max_output_tokens=self._llm.max_output_tokens,
            trace=trace,
            replay=self._replay,
            anthropic_client=anthropic_client,
            effort=self._llm.effort,
            extra_body=self._llm.extra_body,
        )
        try:
            response = await self._adapter.system_one(state, questions, model=provider)
        except AgentBudgetExceeded as error:
            raise PacdsError(504, "agent_budget_exceeded", f"investigation stopped: {error}") from error
        except TypeSafeAPIResponseValidationError as error:
            raise PacdsError(500, "malformed_answer", "the engine could not produce valid answers") from error
        except TypeSafeAPIError as error:
            logger.warning("language model request failed: %s", error)
            if error.status in OVERLOADED_STATUSES:
                raise PacdsError(529, "overloaded", "the language model is overloaded; retry later") from error
            raise PacdsError(500, "engine_error", "the language model request failed") from error
        except TypeSafeAPIConnectionError as error:
            logger.warning("language model unreachable: %s", error)
            raise PacdsError(529, "overloaded", "the language model is unreachable; retry later") from error
        except TypeSafeError as error:
            logger.warning("engine failed: %s", error)
            raise PacdsError(500, "engine_error", "the engine failed") from error
        usage = response.usage
        if trace is not None:
            trace.info["answers"] = {qid: answer.model_dump(mode="json") if hasattr(answer, "model_dump") else answer
                                     for qid, answer in response.answers.items()}
        return Evaluation(
            answers=dict(response.answers),
            input_tokens=usage.input_tokens_total or 0,
            output_tokens=usage.output_tokens_total or 0,
        )
