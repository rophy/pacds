"""Targeted runs: pick cases from a finished run (--from-run/--select), plus a fixed regression sample.

Filters (repeatable --select; all must hold):
  misses              right in at most half its repeats
  errors              failed, undecided or with a failed PACDS call in any repeat (e.g. a usage limit): re-run just those
  class=D[,C]         by ground-truth class
  tier=certain        by review tier
  flipped=OTHER_RUN   majority outcome differs from the same evaluation in OTHER_RUN
A run may be several runs of one milestone: RUN1,RUN2,...
  all                 every case of the evaluation
The regression sample (<cases dir>/regression-sample.json, stratified by class) is added to every targeted run so a
fix for one class cannot silently break another.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tests.analysis.load import Evaluation, Run, failed, load_run, load_runs, parse_runs
from tests.analysis.report import case_majority

REGRESSION_FILE = "regression-sample.json"
REVIEWED_TIERS = ("certain", "probable")


def resolve_evaluation(from_run: Path, kind: str, variant: str) -> tuple[Run, Evaluation]:
    """A results file, or the one evaluation in a run directory of this kind (and variant, when that decides)."""
    paths = parse_runs(from_run)
    if len(paths) == 1 and paths[0].is_file():
        run = load_run(paths[0].parent)
        return run, run.evaluation(paths[0].stem)
    run = load_runs(paths)
    same_kind = [e for e in run.evaluations if e.kind == kind]
    matches = [e for e in same_kind if e.variant == variant] or same_kind
    if len(matches) != 1:
        names = ", ".join(e.label for e in run.evaluations) or "none"
        example = (same_kind or run.evaluations)[0].name if run.evaluations else "support-1"
        raise ValueError(f"{run.name}: cannot tell which evaluation to select from ({names}); pass the results file, e.g. {from_run}/{example}.json")
    return run, matches[0]


def select_cases(evaluation: Evaluation, specs: list[str]) -> list[str]:
    grouped = evaluation.by_case()
    selected = set(grouped)
    for spec in specs:
        key, _, value = spec.partition("=")
        if spec == "all":
            keep = set(grouped)
        elif spec == "errors":
            keep = {case for case, attempts in grouped.items() if any(failed(a) for a in attempts)}
        elif spec == "misses":
            keep = {case for case, attempts in grouped.items() if not case_majority(attempts)}
        elif key == "class" and value:
            keep = {case for case, attempts in grouped.items() if attempts[0].truth in value.split(",")}
        elif key == "tier" and value:
            keep = {case for case in grouped if evaluation.cases.get(case, {}).get("tier") in value.split(",")}
        elif key == "flipped" and value:
            _, other = resolve_evaluation(Path(value), evaluation.kind, evaluation.variant)
            theirs = other.by_case()
            keep = {case for case, attempts in grouped.items() if case in theirs and case_majority(attempts) != case_majority(theirs[case])}
        else:
            raise ValueError(f"unknown --select filter {spec!r} (misses, errors, class=X, tier=X, flipped=RUN, all)")
        selected &= keep
    return sorted(selected)


def load_regression_sample(cases_dir: Path) -> list[str]:
    path = Path(cases_dir) / REGRESSION_FILE
    return list(json.loads(path.read_text())["cases"]) if path.is_file() else []


def draw_regression_sample(cases: list[Any], per_class: int = 3) -> list[str]:
    """Up to per_class reviewed cases per class, certain before probable, in a fixed pseudo-random order."""
    reviewed = [case for case in cases if case.tier in REVIEWED_TIERS]
    chosen: list[str] = []
    for truth in sorted({case.truth for case in reviewed}):
        ranked = sorted((case for case in reviewed if case.truth == truth),
                        key=lambda case: (REVIEWED_TIERS.index(case.tier), hashlib.sha256(case.id.encode()).hexdigest()))
        chosen += [case.id for case in ranked[:per_class]]
    return chosen


def write_regression_sample(cases_dir: Path, case_ids: list[str], per_class: int) -> Path:
    path = Path(cases_dir) / REGRESSION_FILE
    path.write_text(json.dumps({
        "description": f"Regression sample added to every targeted run: up to {per_class} reviewed cases per class, certain first "
                       "(python -m tests.analysis sample). Redraw only deliberately: comparisons across runs assume the same sample.",
        "drawn": datetime.now(UTC).date().isoformat(), "per_class": per_class, "cases": case_ids,
    }, indent=2) + "\n")
    return path


def targeted_case_ids(from_run: Path, specs: list[str], *, kind: str, variant: str, cases_dir: Path,
                      regression: bool = True) -> tuple[list[str], dict[str, Any]]:
    """The case ids for a targeted run, and a description of the selection for its results file."""
    if not specs:
        raise ValueError("--from-run needs at least one --select filter (e.g. --select misses)")
    run, evaluation = resolve_evaluation(from_run, kind, variant)
    chosen = select_cases(evaluation, specs)
    extra = [case for case in load_regression_sample(cases_dir) if case not in chosen] if regression else []
    return chosen + extra, {"from_run": run.name, "evaluation": evaluation.name, "select": specs, "selected": chosen, "regression": extra}
