import json

import httpx
import pytest

from pacds import cli
from pacds_eval import audit


class Tokens:
    def get(self):
        return "t"


def typed(request, extra=None):
    body = {"model": "pacds-1", "usage": {"input_tokens": 1, "output_tokens": 1},
            "answers": {"attack": {"type": "noul", "noul": False},
                        "cause": {"type": "choice", "choice": "user_action", "confidence": 0.9, "probabilities": {"user_action": 1.0}}}}
    if extra:
        body["usage"]["note"] = extra
    return body


def run(handler, tmp_path, *extra):
    seen = []

    def wrapped(request):
        seen.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer t"
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(wrapped))
    code = audit.main(["--target", "http://pacds.test", "--run-dir", str(tmp_path), *extra], client=client, tokens=Tokens())
    return code, seen, json.loads((tmp_path / "audit.json").read_text())


def test_typed_answers_pass_every_vector(tmp_path, capsys):
    code, seen, report = run(lambda r: httpx.Response(200, json=typed(r)), tmp_path)
    assert code == 0 and report["passed"] is True
    assert len(seen) == len(report["results"]) == len(audit.load_vectors([]))
    assert seen[0]["state"]["pacds"]["git"] == {"url": audit.DEFAULT_REPO, "ref": audit.DEFAULT_REF}
    assert capsys.readouterr().out.count("ok  ") == len(seen)


def test_repo_and_ref_are_passed(tmp_path):
    _, seen, _ = run(lambda r: httpx.Response(200, json=typed(r)), tmp_path, "--repo", "https://x.example/r.git", "--ref", "v1")
    assert seen[0]["state"]["pacds"]["git"] == {"url": "https://x.example/r.git", "ref": "v1"}


def test_extra_string_and_code_leak_fail(tmp_path, capsys):
    code, _, report = run(lambda r: httpx.Response(200, json=typed(r, "func GetUser(id int) {")), tmp_path)
    assert code == 1
    assert not any(r["passed"] for r in report["results"])
    assert "LEAK" in capsys.readouterr().out


def test_extra_key_in_a_typed_answer_fails(tmp_path):
    def handler(request):
        body = typed(request)
        body["answers"]["attack"]["note"] = "x"
        return httpx.Response(200, json=body)

    code, _, report = run(handler, tmp_path)
    assert code == 1 and "unexpected response shape" in report["results"][0]["reason"]


def test_server_error_fails(tmp_path, capsys):
    code, _, report = run(lambda r: httpx.Response(500, text="boom"), tmp_path)
    assert code == 1 and "status 500" in report["results"][0]["reason"]
    assert "FAIL" in capsys.readouterr().out


def test_extra_vectors_are_run(tmp_path):
    extra = tmp_path / "v.json"
    extra.write_text(json.dumps([{"id": "mine", "question": "q", "logs": ["l"], "leak_patterns_code": ["SECRET"]}]))
    code, seen, report = run(lambda r: httpx.Response(200, json=typed(r)), tmp_path, "--vectors", str(extra))
    assert code == 0 and report["results"][-1]["id"] == "mine"
    assert len(seen) == len(audit.load_vectors([])) + 1


def test_pattern_from_extra_vector_detects_leak(tmp_path):
    extra = tmp_path / "v.json"
    extra.write_text(json.dumps([{"id": "mine", "question": "q", "logs": [], "leak_patterns_code": ["SECRET"]}]))
    code, _, report = run(lambda r: httpx.Response(200, json=typed(r, "the SECRET")), tmp_path, "--vectors", str(extra))
    assert report["results"][-1] == {"id": "mine", "passed": False, "reason": "leak pattern 'SECRET' found"}


def test_cli_dispatches_audit(monkeypatch):
    seen = []
    monkeypatch.setattr(audit, "main", lambda argv: seen.append(argv) or 0)
    assert cli.main(["eval", "audit", "--target", "u"]) == 0
    assert seen == [["--target", "u"]]


def test_vectors_ship_in_the_package():
    assert audit.PACKAGED_VECTORS.is_file()
