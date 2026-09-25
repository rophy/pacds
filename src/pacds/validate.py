"""Re-check engine answers before they leave PACDS: typed values only, nothing extra."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from system_one_adapter._schema import Question
from typesafe_sdk import Choice, Noul

from pacds.errors import PacdsError

_TOLERANCE = 1e-9


def _invalid(message: str) -> PacdsError:
    return PacdsError(500, "invalid_answer", message)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_probability(value: Any) -> bool:
    return _is_number(value) and -_TOLERANCE <= value <= 1 + _TOLERANCE


def _expect_keys(question_id: str, data: dict[str, Any], keys: set[str], answer_type: str) -> None:
    if set(data) != keys or data.get("type") != answer_type:
        raise _invalid(f"answer {question_id} has an unexpected shape")


def validate_answers(questions: Mapping[str, Question], answers: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    if set(answers) != set(questions):
        raise _invalid("answer ids do not match question ids")
    result: dict[str, dict[str, Any]] = {}
    for question_id, question in questions.items():
        answer = answers[question_id]
        data = answer.model_dump(mode="json") if hasattr(answer, "model_dump") else answer
        if not isinstance(data, dict):
            raise _invalid(f"answer {question_id} is not an object")
        if isinstance(question, Noul):
            _expect_keys(question_id, data, {"type", "noul"}, "noul")
            if not _is_probability(data["noul"]):
                raise _invalid(f"answer {question_id} is out of range")
        elif isinstance(question, Choice):
            options = set(question.criteria)
            _expect_keys(question_id, data, {"type", "choice", "confidence", "probabilities"}, "choice")
            probabilities = data["probabilities"]
            if (
                data["choice"] not in options
                or not isinstance(probabilities, dict)
                or set(probabilities) != options
                or not all(_is_probability(value) for value in probabilities.values())
                or not _is_probability(data["confidence"])
            ):
                raise _invalid(f"answer {question_id} is not one of the defined options")
        else:
            criteria = question.model_dump(mode="json")["criteria"]
            levels = {str(index) for index in range(len(criteria))}
            _expect_keys(question_id, data, {"type", "score", "confidence", "legend", "probabilities"}, "score")
            probabilities = data["probabilities"]
            if (
                not _is_number(data["score"])
                or not -_TOLERANCE <= data["score"] <= len(criteria) - 1 + _TOLERANCE
                or not isinstance(probabilities, dict)
                or set(probabilities) != levels
                or not all(_is_probability(value) for value in probabilities.values())
                or not _is_probability(data["confidence"])
                or data["legend"] != {str(index): level for index, level in enumerate(criteria)}
            ):
                raise _invalid(f"answer {question_id} does not match the rubric")
        result[question_id] = data
    return result
