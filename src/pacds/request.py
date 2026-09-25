"""Parse a Jev /v1/systemone request and the PACDS inputs under state.pacds."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator
from system_one_adapter._schema import Question, convert_question_collection_to_validated_api_question_models
from typesafe_sdk import Choice, Score

from pacds.authz import normalize_git_url
from pacds.errors import PacdsError

MAX_CHOICE_OPTIONS = 255
MAX_SCORE_LEVELS = 10

_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}")
_LOG_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


class GitSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    ref: str

    @field_validator("url")
    @classmethod
    def _valid_url(cls, value: str) -> str:
        normalize_git_url(value)
        return value

    @field_validator("ref")
    @classmethod
    def _valid_ref(cls, value: str) -> str:
        if not _REF.fullmatch(value) or ".." in value:
            raise ValueError("ref must be a branch, tag or full commit SHA")
        return value


class LogSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    url: str

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not _LOG_NAME.fullmatch(value):
            raise ValueError("log name may contain only letters, digits, '.', '_' and '-'")
        return value


class PacdsInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    git: GitSource
    logs: list[LogSource] = []


@dataclass(frozen=True)
class ParsedRequest:
    model: str
    inputs: PacdsInputs
    context: dict[str, Any]
    questions: dict[str, Question]


def _invalid(message: str) -> PacdsError:
    return PacdsError(422, "invalid_request", message)


def _describe(error: ValidationError) -> str:
    first = error.errors(include_input=False, include_url=False)[0]
    location = ".".join(str(part) for part in first["loc"])
    return f"{location}: {first['msg']}" if location else first["msg"]


def parse_request(body: Any, *, max_logs: int) -> ParsedRequest:
    if not isinstance(body, dict):
        raise _invalid("request body must be a JSON object")
    unknown = sorted(set(body) - {"state", "model", "questions"})
    if unknown:
        raise _invalid(f"unknown request fields: {', '.join(unknown)}")

    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise _invalid("model is required")

    state = body.get("state")
    if not isinstance(state, dict) or "pacds" not in state:
        raise _invalid("state must be an object containing 'pacds'")
    try:
        inputs = PacdsInputs.model_validate(state["pacds"])
    except ValidationError as error:
        raise _invalid(f"state.pacds.{_describe(error)}") from None
    if len(inputs.logs) > max_logs:
        raise _invalid(f"state.pacds.logs: at most {max_logs} log files are allowed")
    names = [log.name for log in inputs.logs]
    if len(set(names)) != len(names):
        raise _invalid("state.pacds.logs: names must be unique")

    raw_questions = body.get("questions")
    if not isinstance(raw_questions, dict) or not raw_questions:
        raise _invalid("questions must be a non-empty object")
    try:
        questions = convert_question_collection_to_validated_api_question_models(raw_questions)
    except ValidationError as error:
        raise _invalid(f"questions.{_describe(error)}") from None
    except ValueError as error:
        raise _invalid(f"questions: {error}") from None
    for question_id, question in questions.items():
        if isinstance(question, Choice) and len(question.criteria) > MAX_CHOICE_OPTIONS:
            raise _invalid(f"questions.{question_id}: at most {MAX_CHOICE_OPTIONS} options are allowed")
        if isinstance(question, Score) and len(question.criteria) > MAX_SCORE_LEVELS:
            raise _invalid(f"questions.{question_id}: at most {MAX_SCORE_LEVELS} levels are allowed")

    context = {key: value for key, value in state.items() if key != "pacds"}
    return ParsedRequest(model=model, inputs=inputs, context=context, questions=questions)
