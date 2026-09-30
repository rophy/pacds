"""Compare two runs case by case: accuracy, cases that flipped each way, an exact McNemar test, and cost."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from pacds_eval.analysis.load import Evaluation, Run
from pacds_eval.analysis.report import _rate_cell, _table, case_majority, costs, outcomes
from pacds_eval.analysis.stats import mcnemar_exact


def pair_evaluations(a: Run, b: Run, a_name: str | None = None, b_name: str | None = None) -> list[tuple[Evaluation, Evaluation]]:
    """Explicit names, else every (kind, variant) that occurs exactly once in both runs."""
    if a_name or b_name:
        return [(a.evaluation(a_name or b_name or ""), b.evaluation(b_name or a_name or ""))]

    def unique(run: Run) -> dict[tuple[str, str], Evaluation]:
        keys = [(e.kind, e.variant) for e in run.evaluations]
        return {(e.kind, e.variant): e for e in run.evaluations if keys.count((e.kind, e.variant)) == 1}

    left, right = unique(a), unique(b)
    return [(left[key], right[key]) for key in left if key in right]


def compare_evaluations(run_a: Run, a: Evaluation, run_b: Run, b: Evaluation) -> dict[str, Any]:
    cases_a, cases_b = a.by_case(), b.by_case()
    unpaired_a, unpaired_b = sorted(set(cases_a) - set(cases_b)), sorted(set(cases_b) - set(cases_a))
    shared = sorted(set(cases_a) & set(cases_b))
    only_a = [c for c in shared if case_majority(cases_a[c]) and not case_majority(cases_b[c])]
    only_b = [c for c in shared if case_majority(cases_b[c]) and not case_majority(cases_a[c])]
    # Accuracy and cost over the paired cases only: runs over different case sets are not comparable as a whole.
    a = replace(a, attempts=[attempt for attempt in a.attempts if attempt.case_id in shared])
    b = replace(b, attempts=[attempt for attempt in b.attempts if attempt.case_id in shared])
    outcomes_a, outcomes_b = outcomes(a), outcomes(b)
    costs_a = costs(run_a, a, outcomes_a["overall"]["correct"])
    costs_b = costs(run_b, b, outcomes_b["overall"]["correct"])
    return {
        "a": a.name, "b": b.name, "kind": a.kind, "variant": a.variant, "shared_cases": len(shared),
        "only_in_a": unpaired_a, "only_in_b": unpaired_b,
        "accuracy": {"a": outcomes_a["overall"], "b": outcomes_b["overall"]},
        "by_class": {c: {"a": outcomes_a["by_class"][c], "b": outcomes_b["by_class"].get(c)} for c in outcomes_a["by_class"]},
        "flipped_to_wrong": only_a, "flipped_to_right": only_b,
        "mcnemar_p": mcnemar_exact(len(only_a), len(only_b)),
        "input_per_attempt": {"a": costs_a["input_per_attempt"], "b": costs_b["input_per_attempt"]},
        "input_per_correct": {"a": costs_a["input_per_correct"], "b": costs_b["input_per_correct"]},
    }


def compare(run_a: Run, run_b: Run, a_name: str | None = None, b_name: str | None = None) -> list[dict[str, Any]]:
    pairs = pair_evaluations(run_a, run_b, a_name, b_name)
    if not pairs:
        raise ValueError("no evaluations to pair: name them with --a/--b")
    return [compare_evaluations(run_a, a, run_b, b) for a, b in pairs]


def render(run_a: Run, run_b: Run, results: list[dict[str, Any]]) -> str:
    lines = [f"# Comparison: A = {run_a.name}, B = {run_b.name}", "",
             "Cases are paired by id; a case is right when right in more than half its repeats. "
             "McNemar is exact and two-sided on the cases that flipped.", ""]
    for r in results:
        lines += [f"## {r['kind']} {r['variant']}: {r['a']} vs {r['b']} ({r['shared_cases']} shared cases)", ""]
        rows = [["all", _rate_cell(r["accuracy"]["a"]), _rate_cell(r["accuracy"]["b"])]]
        rows += [[f"class {c}", _rate_cell(v["a"]), _rate_cell(v["b"]) if v["b"] else "-"] for c, v in r["by_class"].items() if v["a"]["n"]]
        lines += _table(["group", "A", "B"], rows)
        lines += ["", f"- Right only in A (flipped to wrong in B): {len(r['flipped_to_wrong'])} {' '.join(r['flipped_to_wrong'])}",
                  f"- Right only in B (flipped to right in B): {len(r['flipped_to_right'])} {' '.join(r['flipped_to_right'])}",
                  f"- McNemar p = {r['mcnemar_p']:.3f}",
                  f"- Input tokens per attempt: A {_tokens(r['input_per_attempt']['a'])}, B {_tokens(r['input_per_attempt']['b'])}; "
                  f"per correct decision: A {_tokens(r['input_per_correct']['a'])}, B {_tokens(r['input_per_correct']['b'])}"]
        if r["only_in_a"] or r["only_in_b"]:
            lines.append(f"- Not paired, left out of everything above: {len(r['only_in_a'])} cases only in A, {len(r['only_in_b'])} only in B")
        lines.append("")
    return "\n".join(lines)



def _tokens(value: float | None) -> str:
    return f"{value:,.0f}" if value else "-"
