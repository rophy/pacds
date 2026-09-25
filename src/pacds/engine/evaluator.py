"""Run TypeSafe's adapter with our AgentProvider and map failures to PACDS errors."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

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
from pacds.engine.agent_provider import AgentBudgetExceeded, AgentProvider
from pacds.engine.tools import WorkspaceTools
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
    def __init__(self, llm: LLMConfig, *, client: openai.AsyncOpenAI | None = None) -> None:
        self._llm = llm
        self._client = client or openai.AsyncOpenAI(base_url=llm.base_url, api_key=llm.api_key, max_retries=0, timeout=120)
        self._adapter = AsyncSystemOneAdapterClient(
            structured_outputs=True,
            llm_answer_mode="probabilities",
            normalize_probabilities=True,
            n_retry_malformed_structure=2,
            retry=RetryPolicy(max_retries=1, timeout=None, http_statuses=set(), predicate=_worth_retrying),
        )

    async def evaluate(self, state: dict[str, Any], questions: dict[str, Question], tools: WorkspaceTools) -> Evaluation:
        client = self._client
        if self._llm.session_header:
            client = client.with_options(default_headers={self._llm.session_header: str(uuid.uuid4())})
        provider = AgentProvider(
            model_name=self._llm.model,
            client=client,
            tools=tools,
            max_turns=self._llm.max_turns,
            time_budget_seconds=self._llm.time_budget_seconds,
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
        return Evaluation(
            answers=dict(response.answers),
            input_tokens=usage.input_tokens_total or 0,
            output_tokens=usage.output_tokens_total or 0,
        )
