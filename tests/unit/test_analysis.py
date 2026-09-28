import json
from pathlib import Path

import pytest

from pacds.engine.trace import Trace
from tests.analysis.checks import matched_checks
from tests.analysis.compare import compare
from tests.analysis.load import load_run
from tests.analysis.report import build, case_records, input_attribution, outcomes, render, write_report
from tests.analysis.select import draw_regression_sample, resolve_evaluation, select_cases, targeted_case_ids
from tests.analysis.stats import mcnemar_exact, median, wilson
from tests.replay.harness import Case

CASES = [{"id": "c1", "truth": "B", "set": "hard", "tier": "certain"}, {"id": "c2", "truth": "D", "set": "hard", "tier": "probable"}]
TAXONOMY = {"classes": {"A": "other_system", "B": "user_error", "C": "infrastructure", "D": "bug"}, "escalate": ["D"]}


class Usage:
    def __init__(self, prompt, completion, cached=0):
        self.prompt_tokens, self.completion_tokens = prompt, completion
        self.prompt_tokens_details = type("D", (), {"cached_tokens": cached})()
        self.completion_tokens_details = None


class Response:
    def __init__(self, content, usage, tool_calls=None, finish="stop"):
        message = type("M", (), {"content": content, "tool_calls": tool_calls})()
        self.choices = [type("C", (), {"message": message, "finish_reason": finish})()]
        self.usage, self.model = usage, "m"


def pacds_trace(request_id: str, *, input_tokens=(100, 300), cached=(0, 90)) -> dict:
    """A two-call investigation: one read_file, then the final answer."""
    trace = Trace(request_id=request_id, status=200, error=None, questions={"q": {"instructions": "Is it deliberate?"}},
                  final={"validated": {"q": {"noul": 0.2}}}, investigation={"turns": 1, "reason": "ready"})
    call = {"id": "t1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}}
    first = [{"role": "system", "content": "s" * 100}, {"role": "user", "content": "q" * 100}, {"role": "user", "content": "d" * 200}]
    record = trace.start_call("investigate", first, {})
    tool_call = type("T", (), {"id": "t1", "function": type("F", (), {"name": "read_file", "arguments": '{"path": "a.py"}'})()})()
    record.attempt(0.0)
    record.respond(Response(None, Usage(input_tokens[0], 5, cached[0]), [tool_call], "tool_calls"), 0.0)
    trace.add_tool("read_file", '{"path": "a.py"}', "x" * 600, 0.0)
    second = [*first, {"role": "assistant", "content": "", "tool_calls": [call]}, {"role": "tool", "tool_call_id": "t1", "content": "x" * 600}]
    record = trace.start_call("final", second, {})
    record.attempt(0.0)
    record.respond(Response('{"q": 0.2}', Usage(input_tokens[1], 7, cached[1])), 0.0)
    return trace.to_dict()


def write_run(root: Path, name: str, decisions: dict[tuple[str, int], str]) -> Path:
    run = root / name
    (run / "traces" / "pacds").mkdir(parents=True)
    (run / "traces" / "support-1").mkdir()
    (run / "run.json").write_text(json.dumps({"commit": "c" * 40, "llm": {"model": "m"}}))
    rows = []
    for (case_id, repeat), decision in decisions.items():
        truth = next(c["truth"] for c in CASES if c["id"] == case_id)
        request_id = f"{name}-{case_id}-{repeat}"
        (run / "traces" / "pacds" / f"{request_id}.json").write_text(json.dumps(pacds_trace(request_id)))
        client = Trace(case_id=case_id)
        client.calls.append({"n": 1, "phase": "agent", "kept": 0, "messages_added": [], "usage": {"input": 50, "output": 5, "cached": 10, "reasoning": 0},
                             "response": {"finish_reason": "tool_calls"}, "attempts": [{"status": 200}]})
        (run / "traces" / "support-1" / f"{case_id}-{repeat}.json").write_text(json.dumps(client.to_dict()))
        rows.append({"case_id": case_id, "truth": truth, "set": "hard", "tier": "certain", "decision": decision, "escalate": decision == "D",
                     "confidence": 0.8, "repeat": repeat, "error": None, "input_tokens": 50,
                     "pacds_requests": [{"request_id": request_id, "questions": {"q": {"type": "noul", "instructions": "Is this intended behavior?"}}}]})
    (run / "support-1.json").write_text(json.dumps({"variant": "full", "repeat": 3, "cases_dir": "tests/replay/cases", "taxonomy": TAXONOMY,
                                                    "cases": CASES, "results": rows}))
    return run


# c1 (B): right 2 of 3; c2 (D): right 1 of 3.
DECISIONS = {("c1", 1): "B", ("c1", 2): "B", ("c1", 3): "C", ("c2", 1): "D", ("c2", 2): "C", ("c2", 3): "C"}


def test_stats():
    low, high = wilson(5, 10)
    assert round(low, 3) == 0.237 and round(high, 3) == 0.763
    assert wilson(0, 0) == (0.0, 1.0)
    assert mcnemar_exact(0, 5) == pytest.approx(0.0625) and mcnemar_exact(3, 3) == 1.0 and mcnemar_exact(0, 0) == 1.0
    assert median([3, 1, 2]) == 2 and median([1, 2, 3, 4]) == 2.5 and median([]) is None


def test_outcomes_count_attempts_cases_and_escalation(tmp_path):
    evaluation = load_run(write_run(tmp_path, "r1", DECISIONS)).evaluation("support-1")
    result = outcomes(evaluation)
    assert (result["overall"]["correct"], result["overall"]["n"]) == (3, 6)
    assert result["by_class"]["B"]["correct"] == 2 and result["by_class"]["D"]["correct"] == 1
    assert result["per_case"]["c1"]["majority"] is True and result["per_case"]["c2"]["majority"] is False
    assert (result["cases"]["correct"], result["cases"]["n"]) == (1, 2)
    assert result["confusion"]["D"]["C"] == 2
    assert result["escalation"]["correct"] == 4  # c1 never escalated (3 right); c2 escalated once (1 right)


def test_input_attribution_splits_tokens_and_counts_resends():
    trace = pacds_trace("r")
    attribution = input_attribution(trace["calls"])
    assert sum(attribution["by_source"].values()) == pytest.approx(400, abs=2)
    assert attribution["by_source"]["tool:read_file"] > attribution["by_source"]["system"]
    # Call 2 resends the first three messages (400 of its ~1,100 characters) and no tool result yet.
    assert 0 < attribution["resent"] < 300 and attribution["resent_tool_results"] == 0


def test_report_links_traces_and_writes_files(tmp_path):
    run = load_run(write_run(tmp_path, "r1", DECISIONS))
    report = build(run)
    [evaluation] = report["evaluations"]
    costs = evaluation["costs"]
    assert costs["pacds_requests"] == 6 and costs["pacds"]["input"] == 6 * 400 and costs["pacds"]["cached"] == 6 * 90
    assert costs["client"]["input"] == 6 * 50 and costs["input_per_correct"] == (6 * 400 + 6 * 50) / 3
    assert set(costs["pacds_by_phase"]) == {"investigate", "final"}
    assert evaluation["behavior"]["checks"]["deliberate"]["tickets"] == 6
    assert evaluation["behavior"]["pacds"]["files_read_median"] == 1
    assert report["pacds_traces_unlinked"] == []
    out = write_report(run)
    assert "support-1: support full" in (out / "report.md").read_text()
    lines = (out / "cases.jsonl").read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["attempts"][0]["pacds"][0]["files_read"] == ["a.py"]


def test_results_files_from_before_self_description_still_load(tmp_path):
    run = tmp_path / "old"
    run.mkdir()
    (run / "replay-1.json").write_text(json.dumps({"results": [
        {"case_id": "c1", "truth": "B", "predicted": "B", "correct": True, "set": "hard", "tier": "certain", "tokens": 10},
        {"case_id": "c2", "truth": "D", "predicted": None, "correct": False, "error": "504", "set": "hard", "tier": "probable"}]}))
    loaded = load_run(run)
    [evaluation] = loaded.evaluations
    assert (evaluation.kind, evaluation.variant, evaluation.repeat) == ("replay", "pacds", 1)
    assert list(evaluation.taxonomy["classes"]) == ["A", "B", "C", "D"]
    assert outcomes(evaluation)["undecided"] == 1
    assert "replay-1" in render(build(loaded))
    assert case_records(loaded, evaluation)[1]["attempts"][0]["error"] == "504"


def test_select_filters_intersect(tmp_path):
    run = write_run(tmp_path, "r1", DECISIONS)
    _, evaluation = resolve_evaluation(run, "support", "full")
    assert select_cases(evaluation, ["misses"]) == ["c2"]
    assert select_cases(evaluation, ["class=B,D"]) == ["c1", "c2"]
    assert select_cases(evaluation, ["misses", "class=B"]) == []
    assert select_cases(evaluation, ["all"]) == ["c1", "c2"]
    with pytest.raises(ValueError, match="unknown"):
        select_cases(evaluation, ["nonsense"])


def test_flipped_compares_with_another_run(tmp_path):
    first = write_run(tmp_path, "r1", DECISIONS)
    second = write_run(tmp_path, "r2", {**DECISIONS, ("c2", 2): "D"})  # c2 now right 2 of 3
    _, evaluation = resolve_evaluation(second / "support-1.json", "support", "full")
    assert select_cases(evaluation, [f"flipped={first}"]) == ["c2"]


def test_targeted_runs_add_the_regression_sample(tmp_path):
    run = write_run(tmp_path, "r1", DECISIONS)
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    (cases_dir / "regression-sample.json").write_text(json.dumps({"cases": ["c2", "r9"]}))
    ids, selection = targeted_case_ids(run, ["class=B"], kind="support", variant="full", cases_dir=cases_dir)
    assert ids == ["c1", "c2", "r9"] and selection["selected"] == ["c1"] and selection["regression"] == ["c2", "r9"]
    ids, _ = targeted_case_ids(run, ["class=B"], kind="support", variant="full", cases_dir=cases_dir, regression=False)
    assert ids == ["c1"]
    with pytest.raises(ValueError, match="--select"):
        targeted_case_ids(run, [], kind="support", variant="full", cases_dir=cases_dir)


def test_ambiguous_run_directory_needs_a_results_file(tmp_path):
    run = write_run(tmp_path, "r1", DECISIONS)
    (run / "support-2.json").write_text((run / "support-1.json").read_text())
    with pytest.raises(ValueError, match="support-1.json|support-2.json"):
        resolve_evaluation(run, "support", "full")


def test_regression_sample_is_stratified_certain_first_and_stable():
    cases = [Case(id=f"{truth}{n}", truth=truth, repo="u", ref="r", report="x", tier=tier)
             for truth in "BD" for n, tier in enumerate(["probable", "certain", "probable", "probable", "unreviewed"])]
    chosen = draw_regression_sample(cases, per_class=3)
    assert len(chosen) == 6 and chosen == draw_regression_sample(list(reversed(cases)), per_class=3)
    assert "B1" in chosen and "D1" in chosen and "B4" not in chosen


def test_compare_pairs_cases_and_counts_flips(tmp_path):
    first = load_run(write_run(tmp_path, "r1", DECISIONS))
    second = load_run(write_run(tmp_path, "r2", {**DECISIONS, ("c2", 2): "D", ("c1", 2): "A"}))
    [result] = compare(first, second)
    assert result["flipped_to_right"] == ["c2"] and result["flipped_to_wrong"] == ["c1"]
    assert result["mcnemar_p"] == 1.0 and result["shared_cases"] == 2


def test_question_checks():
    assert matched_checks({"q": {"instructions": "Is this behavior intended by design?"}}) == {"deliberate"}
    assert "regression" in matched_checks("Did a recent change in the upgrade alter this?")
    assert "ownership" in matched_checks("Who controls the database grants?")


def test_compare_uses_paired_cases_only(tmp_path):
    first = load_run(write_run(tmp_path, "r1", {("c1", 1): "B"}))
    second = load_run(write_run(tmp_path, "r2", {("c1", 1): "C", ("c2", 1): "D"}))
    [result] = compare(first, second)
    assert result["only_in_b"] == ["c2"] and result["accuracy"]["b"]["n"] == 1 and result["accuracy"]["b"]["correct"] == 0


def test_several_runs_form_one_milestone(tmp_path):
    from tests.analysis.load import load_runs

    first = write_run(tmp_path, "r1", {("c1", 1): "B", ("c2", 1): "C"})
    second = write_run(tmp_path, "r2", {("c1", 1): "B", ("c2", 1): "D"})
    run = load_runs([first, second])
    evaluation = run.evaluation("support-1")
    assert evaluation.repeat == 6 and sorted((a.case_id, a.repeat) for a in evaluation.attempts) == [("c1", 1), ("c1", 4), ("c2", 1), ("c2", 4)]
    assert len(run.pacds_traces) == 4 and run.info["runs"] == ["r1", "r2"]
    out = write_report(run, tmp_path / "milestone")
    assert "r1+r2" in (out / "report.md").read_text()


def test_a_rerun_of_errors_replaces_the_failed_attempts(tmp_path):
    from tests.analysis.load import load_runs

    first = write_run(tmp_path, "r1", {("c1", 1): "B", ("c2", 1): "D"})
    data = json.loads((first / "support-1.json").read_text())
    data["results"][1].update(decision=None, error="429 usage limit")
    (first / "support-1.json").write_text(json.dumps(data))
    rerun = write_run(tmp_path, "r2", {("c2", 1): "D", ("c1", 1): "C"})
    data = json.loads((rerun / "support-1.json").read_text())
    data["selection"] = {"select": ["errors"]}
    (rerun / "support-1.json").write_text(json.dumps(data))
    evaluation = load_runs([first, rerun]).evaluation("support-1")
    assert [(a.case_id, a.decision, a.error) for a in evaluation.attempts] == [("c1", "B", None), ("c2", "D", None)]


def test_errors_filter_picks_failed_or_undecided(tmp_path):
    run = write_run(tmp_path, "r1", DECISIONS)
    data = json.loads((run / "support-1.json").read_text())
    data["results"][4].update(decision=None, error="boom")
    (run / "support-1.json").write_text(json.dumps(data))
    _, evaluation = resolve_evaluation(run, "support", "full")
    assert select_cases(evaluation, ["errors"]) == ["c2"]


def test_a_ticket_decided_after_a_failed_pacds_call_counts_as_an_error(tmp_path):
    run = write_run(tmp_path, "r1", {("c1", 1): "B", ("c2", 1): "D"})
    data = json.loads((run / "support-1.json").read_text())
    data["results"][0]["pacds_errors"] = 1
    (run / "support-1.json").write_text(json.dumps(data))
    _, evaluation = resolve_evaluation(run, "support", "full")
    assert select_cases(evaluation, ["errors"]) == ["c1"]


def test_report_counts_replayed_calls_apart(tmp_path):
    run = write_run(tmp_path, "r1", {("c1", 1): "B"})
    [path] = (run / "traces" / "pacds").glob("*.json")
    trace = json.loads(path.read_text())
    trace["calls"][0]["replayed"] = True
    path.write_text(json.dumps(trace))
    costs = build(load_run(run))["evaluations"][0]["costs"]
    assert costs["calls"]["pacds"] == {"live": 1, "replayed": 1} and costs["live_input"] == 300 + 50
    assert "Replayed model calls: PACDS 1 of 2" in render(build(load_run(run)))


def _miss_run(tmp_path):
    run = write_run(tmp_path, "r1", {("c1", 1): "C", ("c2", 1): "C", ("c1", 2): "B"})
    data = json.loads((run / "support-1.json").read_text())
    data["results"][1]["pacds_requests"] = []  # c2 repeat 1: decided without asking PACDS
    data["results"][0]["transcript"] = [{"role": "user", "content": "ticket"}, {"role": "assistant", "content": "", "tool_calls": [
        {"id": "t", "type": "function", "function": {"name": "call_pacds", "arguments": '{"questions": {}}'}}]}]
    (run / "support-1.json").write_text(json.dumps(data))
    return run


def test_classify_rules_llm_and_cache(tmp_path, monkeypatch):
    from tests.analysis import classify as module

    run = load_run(_miss_run(tmp_path))
    evaluation = run.evaluation("support-1")
    asked = []

    async def fake_ask(client, model, api, text):
        asked.append(json.loads(text))
        return {"mode": "wrong_questions", "deciding_fact": "who owns the grant", "explanation": "never asked"}, {"input": 10, "output": 2}

    monkeypatch.setattr(module, "_ask", fake_ask)
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setenv("LLM_MODEL", "luna")
    root = tmp_path / "report"
    results = module.classify(run, [evaluation], root)
    assert sorted((r["case_id"], r["mode"]) for r in results) == [("c1", "wrong_questions"), ("c2", "no_pacds")]
    [dossier] = asked
    assert dossier["true_class"].startswith("B") and dossier["pacds_investigations"][0]["tool_calls"][0]["name"] == "read_file"
    assert dossier["conversation"][1]["tool_calls"][0]["name"] == "call_pacds"
    # Cached: a second run asks nothing; a new prompt invalidates the cache.
    module.classify(run, [evaluation], root)
    assert len(asked) == 1
    monkeypatch.setattr(module, "prompt", lambda: "a new prompt")
    module.classify(run, [evaluation], root)
    assert len(asked) == 2


def test_failure_modes_appear_in_the_report_and_select(tmp_path, monkeypatch):
    from tests.analysis import classify as module

    path = _miss_run(tmp_path)
    run = load_run(path)

    async def fake_ask(client, model, api, text):
        return {"mode": "pacds_wrong", "deciding_fact": "f", "explanation": "e"}, {}

    monkeypatch.setattr(module, "_ask", fake_ask)
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setenv("LLM_MODEL", "luna")
    module.classify(run, [run.evaluation("support-1")], path / "report")
    out = write_report(run)
    assert "Failure modes" in (out / "report.md").read_text()
    assert json.loads((out / "report.json").read_text())["evaluations"][0]["failure_modes"] == {"B": {"pacds_wrong": 1}, "D": {"no_pacds": 1}}
    _, evaluation = resolve_evaluation(path, "support", "full")
    assert select_cases(evaluation, ["mode=pacds_wrong"]) == ["c1"]


def test_failed_tickets_are_infrastructure_without_a_model_call(tmp_path):
    from tests.analysis.classify import by_rule

    run = write_run(tmp_path, "r1", {("c1", 1): "C"})
    data = json.loads((run / "support-1.json").read_text())
    data["results"][0].update(decision=None, error="429")
    (run / "support-1.json").write_text(json.dumps(data))
    evaluation = load_run(run).evaluation("support-1")
    assert by_rule(evaluation, evaluation.attempts[0]) == "infrastructure"
