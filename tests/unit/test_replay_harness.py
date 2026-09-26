import json

import pytest

from tests.replay.harness import CLASSES, Case, Result, build_request, load_cases, score, summarize


def write_case(root, case_id, truth="D", logs=(), case_set=None):
    directory = root / case_id
    directory.mkdir(parents=True)
    for name in logs:
        (directory / name).write_text("log line\n")
    (directory / "case.json").write_text(json.dumps({
        "id": case_id, "truth": truth, "repo": "https://github.com/o/r.git", "ref": "v1.0.0",
        "report": "It broke", "logs": list(logs), "issue_url": "https://github.com/o/r/issues/1",
        **({"set": case_set} if case_set else {}),
    }))


def test_loads_cases_sorted_and_filters_by_id(tmp_path):
    write_case(tmp_path, "r-2", truth="B")
    write_case(tmp_path, "r-1", truth="A", logs=["issue.log"])
    cases = load_cases(tmp_path)
    assert [(c.id, c.truth, c.logs) for c in cases] == [("r-1", "A", ["issue.log"]), ("r-2", "B", [])]
    assert [c.id for c in load_cases(tmp_path, only=["r-2"])] == ["r-2"]


def test_unknown_case_id_is_an_error(tmp_path):
    write_case(tmp_path, "r-1")
    with pytest.raises(ValueError, match="nope"):
        load_cases(tmp_path, only=["nope"])


def test_case_with_missing_log_file_is_an_error(tmp_path):
    write_case(tmp_path, "r-1")
    data = json.loads((tmp_path / "r-1" / "case.json").read_text())
    data["logs"] = ["missing.log"]
    (tmp_path / "r-1" / "case.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="missing.log"):
        load_cases(tmp_path)


def test_request_carries_repo_report_logs_and_one_choice_question():
    case = Case(id="r-1", truth="C", repo="https://github.com/o/r.git", ref="v1.0.0", report="It broke", logs=["issue.log"])
    body = build_request(case, lambda key: f"https://s3.test/{key}?sig=x")
    assert body["state"] == {
        "pacds": {"git": {"url": "https://github.com/o/r.git", "ref": "v1.0.0"},
                  "logs": [{"name": "issue.log", "url": "https://s3.test/replay/r-1/issue.log?sig=x"}]},
        "user_report": "It broke",
    }
    question = body["questions"]["cause"]
    assert question["type"] == "choice"
    assert set(question["criteria"]) == set(CLASSES.values())


def test_request_without_logs_omits_them():
    case = Case(id="r-1", truth="C", repo="u", ref="main", report="x", logs=[])
    assert "logs" not in build_request(case, lambda key: "never")["state"]["pacds"]


def answer(choice: str, probabilities: dict) -> dict:
    return {"answers": {"cause": {"type": "choice", "choice": choice, "probabilities": probabilities}}}


def test_score_maps_the_choice_back_to_a_class():
    case = Case(id="r-1", truth="D", repo="u", ref="main", report="x", logs=[])
    result = score(case, answer("bug", {"bug": 0.7, "user_error": 0.2, "other_system": 0.05, "infrastructure": 0.05}), tokens=100, seconds=2.5)
    assert (result.predicted, result.correct, result.p_truth, result.error) == ("D", True, 0.7, None)


def test_score_records_wrong_answers():
    case = Case(id="r-1", truth="A", repo="u", ref="main", report="x", logs=[])
    result = score(case, answer("bug", {"bug": 0.6, "other_system": 0.4, "user_error": 0, "infrastructure": 0}), tokens=1, seconds=1)
    assert (result.predicted, result.correct, result.p_truth) == ("D", False, 0.4)


def test_summary_reports_accuracy_confusion_and_errors():
    results = [
        Result(case_id="1", truth="A", predicted="A", correct=True, p_truth=0.9),
        Result(case_id="2", truth="A", predicted="D", correct=False, p_truth=0.2),
        Result(case_id="3", truth="D", predicted="D", correct=True, p_truth=0.8),
        Result(case_id="4", truth="B", predicted=None, correct=False, p_truth=None, error="504 agent_budget_exceeded"),
    ]
    summary = summarize(results)
    assert summary["accuracy"] == 0.5
    assert summary["errors"] == 1
    assert summary["confusion"]["A"] == {"A": 1, "B": 0, "C": 0, "D": 1}
    assert summary["mean_p_truth"] == pytest.approx((0.9 + 0.2 + 0.8) / 3)


def test_cases_default_to_the_clear_set_and_can_be_filtered_by_set(tmp_path):
    write_case(tmp_path, "r-1")
    write_case(tmp_path, "r-2", case_set="hard")
    assert [(c.id, c.set) for c in load_cases(tmp_path)] == [("r-1", "clear"), ("r-2", "hard")]
    assert [c.id for c in load_cases(tmp_path, sets=["hard"])] == ["r-2"]


def test_unknown_set_is_an_error(tmp_path):
    write_case(tmp_path, "r-1", case_set="medium")
    with pytest.raises(ValueError, match="medium"):
        load_cases(tmp_path)


def test_user_error_option_covers_surprising_but_intended_behavior():
    from tests.replay.harness import CRITERIA

    assert "even if the user did not expect" in CRITERIA["user_error"]


def test_cases_carry_their_review_tier(tmp_path):
    write_case(tmp_path, "r-1")
    data = json.loads((tmp_path / "r-1" / "case.json").read_text())
    data["tier"] = "probable"
    (tmp_path / "r-1" / "case.json").write_text(json.dumps(data))
    write_case(tmp_path, "r-2")
    assert [(c.id, c.tier) for c in load_cases(tmp_path)] == [("r-1", "probable"), ("r-2", "certain")]
