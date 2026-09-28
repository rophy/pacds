"""Load an evaluation run directory: run.json, results files, PACDS traces and client traces."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SKIP = {"run.json", "errors.json", "pacds-config.json"}
# Runs written before results files described their taxonomy (2026-09-28) all used this one.
DEFAULT_TAXONOMY = {"classes": {"A": "other_system", "B": "user_error", "C": "infrastructure", "D": "bug"}, "escalate": ["D"]}
CASE_DIRS = (Path("tests/replay/cases"), Path("tests/replay/candidates"))


@dataclass
class Attempt:
    """One answer to one case: a support ticket, or one fixed-question replay."""

    case_id: str
    repeat: int
    truth: str
    decision: str | None
    escalate: bool | None
    confidence: float | None
    error: str | None
    request_ids: list[str]
    row: dict[str, Any]
    client_trace: dict[str, Any] | None = None

    @property
    def correct(self) -> bool:
        return self.decision is not None and self.decision == self.truth


@dataclass
class Evaluation:
    """One results file: one runner invocation over a set of cases."""

    name: str
    kind: str  # support | replay
    variant: str  # full | no-pacds | pacds | baseline
    repeat: int
    cases: dict[str, dict[str, Any]]
    taxonomy: dict[str, Any]
    cases_dir: Path | None
    attempts: list[Attempt] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.name} ({self.kind} {self.variant})"

    def by_case(self) -> dict[str, list[Attempt]]:
        grouped: dict[str, list[Attempt]] = {}
        for attempt in self.attempts:
            grouped.setdefault(attempt.case_id, []).append(attempt)
        return grouped

    def case_file(self, case_id: str) -> dict[str, Any] | None:
        """The case's case.json, for its reviews (the reviewed fix); None when not available here."""
        for root in ([self.cases_dir] if self.cases_dir else []) + list(CASE_DIRS):
            path = root / case_id / "case.json"
            if path.is_file():
                return json.loads(path.read_text())
        return None


@dataclass
class Run:
    path: Path
    info: dict[str, Any]
    evaluations: list[Evaluation]
    pacds_traces: dict[str, dict[str, Any]]

    @property
    def name(self) -> str:
        return self.path.name

    def evaluation(self, name: str) -> Evaluation:
        for evaluation in self.evaluations:
            if name in (evaluation.name, f"{evaluation.name}.json"):
                return evaluation
        raise ValueError(f"{self.name}: no results file {name!r}; has {', '.join(e.name for e in self.evaluations)}")

    def traces_for(self, attempt: Attempt) -> list[dict[str, Any]]:
        return [self.pacds_traces[rid] for rid in attempt.request_ids if rid in self.pacds_traces]


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def load_evaluation(path: Path) -> Evaluation:
    data = _load_json(path)
    rows = data.get("results", [])
    if "variant" in data:
        kind, variant = "support", data["variant"]
    else:
        kind, variant = "replay", "baseline" if data.get("baseline") else "pacds"
    cases = {case["id"]: case for case in data.get("cases", [])}
    for row in rows:  # results files from before 2026-09-28 do not list their cases
        cases.setdefault(row["case_id"], {"id": row["case_id"], "truth": row["truth"], "set": row.get("set"), "tier": row.get("tier")})
    repeat = data.get("repeat") or max((row.get("repeat", 1) for row in rows), default=1)
    evaluation = Evaluation(name=path.stem, kind=kind, variant=variant, repeat=repeat, cases=cases,
                            taxonomy=data.get("taxonomy") or DEFAULT_TAXONOMY,
                            cases_dir=Path(data["cases_dir"]) if data.get("cases_dir") else None, data=data)
    traces_dir = path.parent / "traces" / path.stem
    for row in rows:
        if kind == "support":
            decision = row.get("decision")
            request_ids = [r["request_id"] for r in row.get("pacds_requests", []) if r.get("request_id")]
        else:
            decision = row.get("predicted")
            request_ids = [row["request_id"]] if row.get("request_id") else []
        repeat_n = row.get("repeat", 1)
        trace_path = traces_dir / f"{row['case_id']}-{repeat_n}.json"
        evaluation.attempts.append(Attempt(
            case_id=row["case_id"], repeat=repeat_n, truth=row["truth"], decision=decision, escalate=row.get("escalate"),
            confidence=row.get("confidence"), error=row.get("error"), request_ids=request_ids, row=row,
            client_trace=_load_json(trace_path) if trace_path.is_file() else None,
        ))
    return evaluation


def load_run(path: Path) -> Run:
    path = Path(path)
    if not path.is_dir():
        raise ValueError(f"{path} is not a run directory (fetch archived runs with python -m tests.eval_run fetch NAME)")
    info = _load_json(path / "run.json") if (path / "run.json").is_file() else {}
    evaluations = [load_evaluation(p) for p in sorted(path.glob("*.json")) if p.name not in SKIP]
    traces_dir = path / "traces" / "pacds"
    traces = {p.stem: _load_json(p) for p in sorted(traces_dir.glob("*.json"))} if traces_dir.is_dir() else {}
    return Run(path=path, info=info, evaluations=evaluations, pacds_traces=traces)
