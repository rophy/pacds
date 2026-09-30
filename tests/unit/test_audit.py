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


def run(handler, tmp_path, *extra, engine=None):
    seen = []

    def wrapped(request):
        if request.method == "GET":
            return httpx.Response(200, json={"models": [{"name": engine}]}) if engine else httpx.Response(404)
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


def good(**over):
    return {"id": "v", "question": "q", "logs": [], **over}


@pytest.mark.parametrize("content", [
    "not json", json.dumps({"id": "x"}), json.dumps(["str"]), json.dumps([{"question": "q", "logs": []}]),
    json.dumps([good(logs="x")]), json.dumps([good(leak_patterns_code="x")]), json.dumps([good(leak_patterns_diagnostic=["("])]),
    json.dumps([good(id="real-getuser-then-ask-code")]), json.dumps([good(), good()]),
])
def test_invalid_vectors_exit_2_before_any_request(tmp_path, capsys, content):
    extra = tmp_path / "v.json"
    extra.write_text(content)
    client = httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("request sent")))
    code = audit.main(["--target", "http://x", "--run-dir", str(tmp_path), "--vectors", str(extra)], client=client, tokens=Tokens())
    assert code == 2 and "invalid vectors" in capsys.readouterr().err
    assert not (tmp_path / "audit.json").exists()


def test_missing_vectors_file_exits_2(tmp_path, capsys):
    code = audit.main(["--target", "http://x", "--run-dir", str(tmp_path), "--vectors", str(tmp_path / "nope.json")], tokens=Tokens())
    assert code == 2 and "nope.json" in capsys.readouterr().err


def test_audit_json_is_written_when_interrupted(tmp_path):
    calls = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(404)
        calls.append(1)
        if len(calls) == 3:
            raise KeyboardInterrupt
        return httpx.Response(200, json=typed(request))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(KeyboardInterrupt):
        audit.main(["--target", "http://x", "--run-dir", str(tmp_path)], client=client, tokens=Tokens())
    report = json.loads((tmp_path / "audit.json").read_text())
    assert report["complete"] is False and report["passed"] is False and len(report["results"]) == 2


def test_token_never_reaches_audit_json_or_stdout(tmp_path, capsys):
    class Secret:
        def get(self):
            return "s3cr3t-token"

    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="denied")))
    audit.main(["--target", "http://x", "--run-dir", str(tmp_path)], client=client, tokens=Secret())
    assert "s3cr3t-token" not in (tmp_path / "audit.json").read_text() + capsys.readouterr().out


def test_finish_and_load_run_ignore_audit_json(tmp_path):
    from pacds_eval.analysis.load import load_run
    from pacds_eval.runs import finish

    (tmp_path / "run.json").write_text("{}")
    (tmp_path / "audit.json").write_text(json.dumps({"passed": True, "complete": True, "results": [{"id": "a"}]}))
    assert finish(tmp_path)["evaluations"] == []
    assert load_run(tmp_path).evaluations == []


def test_engine_name_of_the_target_is_allowed(tmp_path):
    def handler(request):
        body = typed(request)
        body["model"] = "acme-triage"
        return httpx.Response(200, json=body)

    code, _, report = run(handler, tmp_path, engine="acme-triage")
    assert code == 0 and report["passed"] is True


def test_engine_name_falls_back_to_pacds_1_when_models_is_unavailable(tmp_path):
    assert run(lambda r: httpx.Response(200, json=typed(r)), tmp_path, engine=None)[0] == 0

    def other(request):
        body = typed(request)
        body["model"] = "acme-triage"
        return httpx.Response(200, json=body)

    code, _, report = run(other, tmp_path, engine=None)
    assert code == 1 and not any(r["passed"] for r in report["results"])


def test_another_string_is_still_a_leak_with_a_custom_engine_name(tmp_path):
    code, _, _ = run(lambda r: httpx.Response(200, json=typed(r, "secret text")), tmp_path, engine="acme-triage")
    assert code == 1


def test_invalid_vectors_leave_no_run_directory(tmp_path):
    extra = tmp_path / "v.json"
    extra.write_text("not json")
    run_dir = tmp_path / "run"
    assert audit.main(["--target", "http://x", "--run-dir", str(run_dir), "--vectors", str(extra)], tokens=Tokens()) == 2
    assert not run_dir.exists()


def test_default_run_dir_uses_the_runs_root(tmp_path, monkeypatch):
    monkeypatch.setenv("PACDS_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.delenv("EVAL_RUN_DIR", raising=False)
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=typed(r))))
    audit.main(["--target", "http://x"], client=client, tokens=Tokens())
    (created,) = (tmp_path / "runs").iterdir()
    assert (created / "audit.json").exists()
