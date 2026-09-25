import copy

import pytest
from typesafe_sdk import Choice, Noul, Score

from pacds.errors import PacdsError
from pacds.request import parse_request

VALID = {
    "model": "jev-latest",
    "state": {
        "pacds": {
            "git": {"url": "https://git.example.com/shop/checkout.git", "ref": "v2.14.3"},
            "logs": [{"name": "server.log", "url": "https://bucket.s3.amazonaws.com/server.log?X-Amz-Signature=abc"}],
        },
        "user_report": "Checkout fails",
    },
    "questions": {
        "cause": {"type": "choice", "instructions": "What caused it?", "criteria": {"code_defect": None, "user_action": "Bad input"}},
        "urgent": {"type": "noul", "instructions": "Is it urgent?"},
        "severity": {"type": "score", "instructions": "How bad?", "criteria": ["low", "high"]},
    },
}

DELETE = object()


def with_change(path: list, value):
    body = copy.deepcopy(VALID)
    target = body
    for key in path[:-1]:
        target = target[key]
    if value is DELETE:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return body


def test_parses_valid_request():
    parsed = parse_request(VALID, max_logs=10)
    assert parsed.model == "jev-latest"
    assert parsed.inputs.git.ref == "v2.14.3"
    assert parsed.inputs.logs[0].name == "server.log"
    assert parsed.context == {"user_report": "Checkout fails"}
    assert isinstance(parsed.questions["cause"], Choice)
    assert isinstance(parsed.questions["urgent"], Noul)
    assert isinstance(parsed.questions["severity"], Score)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (["state"], "just a string"),
        (["state", "pacds"], DELETE),
        (["state", "pacds", "git"], DELETE),
        (["state", "pacds", "git", "url"], "http://git.example.com/shop/checkout.git"),
        (["state", "pacds", "git", "url"], "https://tok@git.example.com/shop/checkout.git"),
        (["state", "pacds", "git", "ref"], "--upload-pack=evil"),
        (["state", "pacds", "git", "ref"], "main/../x"),
        (["state", "pacds", "git", "extra"], 1),
        (["state", "pacds", "logs"], [{"name": "../etc/passwd", "url": "https://h/x"}]),
        (["state", "pacds", "logs"], [{"name": "a.log", "url": "https://h/1"}, {"name": "a.log", "url": "https://h/2"}]),
        (["model"], DELETE),
        (["questions"], {}),
        (["questions", "cause", "type"], "essay"),
        (["questions", "cause", "criteria"], {"only": None}),
        (["questions", "cause", "criteria"], {f"o{i}": None for i in range(256)}),
        (["questions", "severity", "criteria"], [str(i) for i in range(11)]),
        (["surprise"], True),
    ],
)
def test_rejects_invalid_requests(path, value):
    with pytest.raises(PacdsError) as error:
        parse_request(with_change(path, value), max_logs=10)
    assert error.value.status == 422
    assert error.value.code == "invalid_request"


def test_rejects_too_many_logs():
    with pytest.raises(PacdsError) as error:
        parse_request(VALID, max_logs=0)
    assert error.value.status == 422


def test_error_message_does_not_echo_log_url():
    body = with_change(["state", "pacds", "logs", 0, "name"], "../bad")
    with pytest.raises(PacdsError) as error:
        parse_request(body, max_logs=10)
    assert "X-Amz-Signature" not in error.value.message


def test_accepts_255_choice_options_and_full_sha():
    body = with_change(["questions", "cause", "criteria"], {f"o{i}": None for i in range(255)})
    body["state"]["pacds"]["git"]["ref"] = "a" * 40
    parsed = parse_request(body, max_logs=10)
    assert len(parsed.questions["cause"].criteria) == 255
