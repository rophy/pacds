import json

from tests.eval_run import errors, record


def test_record_keeps_commit_model_and_arguments(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "m1")
    info = record(tmp_path, ["--replay", "--set hard"])
    saved = json.loads((tmp_path / "run.json").read_text())
    assert saved == info and saved["llm"]["model"] == "m1" and saved["args"] == ["--replay", "--set hard"]
    assert len(saved["commit"]) == 40


def test_errors_collects_failed_requests_with_their_ids(tmp_path):
    (tmp_path / "replay-1.json").write_text(json.dumps({"results": [
        {"case_id": "c1", "error": None, "request_id": "r0"},
        {"case_id": "c2", "error": "504 budget", "request_id": "r1"},
    ]}))
    (tmp_path / "support-1.json").write_text(json.dumps({"results": [
        {"case_id": "c3", "error": None, "pacds_requests": [
            {"request_id": "r2", "reply": {"answers": {}}},
            {"request_id": "r3", "reply": {"error": {"status": 500, "code": "engine_error", "message": "the engine failed"}}},
        ]},
        {"case_id": "c4", "error": "APITimeoutError()", "pacds_requests": []},
    ]}))
    (tmp_path / "run.json").write_text("{}")
    found = errors(tmp_path)
    assert [(f["case_id"], f["request_id"], f["code"]) for f in found] == [
        ("c2", "r1", None), ("c3", "r3", "engine_error"), ("c4", None, "agent_failed")]
    assert json.loads((tmp_path / "errors.json").read_text()) == found
