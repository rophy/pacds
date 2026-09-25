import pytest
from typesafe_sdk import Choice, ChoiceAnswer, Noul, NoulAnswer, Score, ScoreAnswer

from pacds.errors import PacdsError
from pacds.validate import validate_answers

QUESTIONS = {
    "cause": Choice(instructions="What caused it?", criteria={"code_defect": None, "user_action": "Bad input"}),
    "urgent": Noul(instructions="Urgent?"),
    "severity": Score(instructions="How bad?", criteria=["low", "high"]),
}


def good_answers() -> dict:
    return {
        "cause": ChoiceAnswer(choice="code_defect", confidence=0.6, probabilities={"code_defect": 0.8, "user_action": 0.2}),
        "urgent": NoulAnswer(noul=0.3),
        "severity": ScoreAnswer(score=0.7, confidence=0.4, probabilities={0: 0.3, 1: 0.7}, legend={0: "low", 1: "high"}),
    }


def test_valid_answers_are_serialized():
    result = validate_answers(QUESTIONS, good_answers())
    assert result["cause"] == {"type": "choice", "choice": "code_defect", "confidence": 0.6, "probabilities": {"code_defect": 0.8, "user_action": 0.2}}
    assert result["urgent"] == {"type": "noul", "noul": 0.3}
    assert result["severity"]["legend"] == {"0": "low", "1": "high"}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda a: a.pop("urgent"),
        lambda a: a.update(extra=NoulAnswer(noul=0.1)),
        lambda a: a.update(cause={"type": "choice", "choice": "leaked_function_name", "confidence": 0.5, "probabilities": {"code_defect": 0.5, "user_action": 0.5}}),
        lambda a: a.update(cause={"type": "choice", "choice": "code_defect", "confidence": 0.5, "probabilities": {"code_defect": 0.5, "user_action": 0.5}, "note": "free text"}),
        lambda a: a.update(urgent={"type": "noul", "noul": 1.5}),
        lambda a: a.update(urgent={"type": "noul", "noul": "yes"}),
        lambda a: a.update(urgent={"type": "choice", "noul": 0.5}),
        lambda a: a.update(severity={"type": "score", "score": 3.0, "confidence": 0.4, "probabilities": {"0": 0.3, "1": 0.7}, "legend": {"0": "low", "1": "high"}}),
        lambda a: a.update(severity={"type": "score", "score": 0.7, "confidence": 0.4, "probabilities": {"0": 0.3, "1": 0.7}, "legend": {"0": "low", "1": "secret"}}),
    ],
)
def test_invalid_answers_are_rejected(mutate):
    answers = good_answers()
    mutate(answers)
    with pytest.raises(PacdsError) as error:
        validate_answers(QUESTIONS, answers)
    assert (error.value.status, error.value.code) == (500, "invalid_answer")
