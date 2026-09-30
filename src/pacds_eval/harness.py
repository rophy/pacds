"""Replay real GitHub support cases through PACDS and score its verdicts (a case set: --cases-dir).

Usage: uv run python -m pacds_eval.harness [--case ID ...] [--concurrency N] [--out results.json] [--baseline]
Needs the Compose dev stack with a real LLM (scripts/eval.sh --replay, or docker compose up + scripts/seed-logs.sh).
--baseline skips PACDS: the same model answers from the report and logs only, using LLM_* from the
environment (e.g. `set -a; . ./.env; set +a`).
"""

from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

import httpx

from pacds.app import REQUEST_ID_HEADER
from pacds_eval.oidc import TokenSource

BASE_URL = os.environ.get("PACDS_URL", "http://localhost:3002")

# Ground-truth classes (A-D) and the option key PACDS answers with for each.
CLASSES = {"A": "other_system", "B": "user_error", "C": "infrastructure", "D": "bug"}
# The incident-triage taxonomy is the client's: PACDS itself only knows how to investigate.
CRITERIA = {
    "other_system": "Not caused by this application but by software the operator does not control: the end user's browser, "
    "OS or extensions, an upstream library, or a third-party service or website. Likely when the code handles the input "
    "correctly yet the symptom occurs.",
    "user_error": "The application works as designed, even if the user did not expect the behavior; the user misused it, "
    "misunderstood a feature, or entered a wrong setting or input.",
    "infrastructure": "The application code is fine, but something the operator of this deployment runs or configures is "
    "misconfigured or failing: reverse proxy, WAF, DNS, network, container, database, storage, file permissions, or server "
    "configuration. Likely when the code handles the input correctly yet the symptom occurs.",
    "bug": "A defect in this application's own code. Choose only when the faulty logic is identified; behavior the code "
    "produces deliberately is not a bug.",
}
QUESTION = "What caused the problem described in the user's report?"
# clear: the report alone mostly decides the class; hard: only the code does (the no-code baseline fails);
# candidate: not yet filtered or reviewed.
SETS = ("clear", "hard", "candidate")


@dataclass(frozen=True)
class Case:
    id: str
    truth: str
    repo: str
    ref: str
    report: str
    logs: list[str] = field(default_factory=list)
    set: str = "clear"
    # certain: both blind reviewers were certain of the class; probable: at least one was not.
    tier: str = "certain"


@dataclass(frozen=True)
class Result:
    case_id: str
    truth: str
    predicted: str | None
    correct: bool
    p_truth: float | None
    tokens: int = 0
    seconds: float = 0.0
    error: str | None = None
    request_id: str | None = None
    repeat: int = 1


def load_cases(root: Path, only: list[str] | None = None, sets: list[str] | None = None, tiers: list[str] | None = None) -> list[Case]:
    cases = []
    for path in sorted(root.glob("*/case.json")):
        data = json.loads(path.read_text())
        # Imported tickets are evaluated once labeled (pacds_eval/casebook.py); older cases carry no status.
        if (data.get("status") or ("labeled" if data.get("truth") else "draft")) != "labeled":
            continue
        for name in data.get("logs", []):
            if not (path.parent / name).is_file():
                raise ValueError(f"{data['id']}: log file {name} is missing")
        case_set = data.get("set", "clear")
        if case_set not in SETS:
            raise ValueError(f"{data['id']}: unknown set {case_set!r}")
        cases.append(Case(id=data["id"], truth=data["truth"], repo=data["repo"], ref=data["ref"], report=data["report"],
                          logs=list(data.get("logs", [])), set=case_set, tier=data.get("tier", "certain")))
    if sets:
        cases = [case for case in cases if case.set in sets]
    if tiers:
        cases = [case for case in cases if case.tier in tiers]
    if only:
        unknown = sorted(set(only) - {case.id for case in cases})
        if unknown:
            raise ValueError(f"unknown case ids: {', '.join(unknown)}")
        cases = [case for case in cases if case.id in only]
    return cases


# Classes whose tickets must go to the development team.
ESCALATE = ("D",)


def run_description(cases: list[Case], cases_dir: Path, repeat: int) -> dict[str, Any]:
    """What a results file evaluated, so analysis needs nothing but the file: cases, labels, taxonomy, repeats."""
    try:
        where = str(cases_dir.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        where = str(cases_dir)
    return {"repeat": repeat, "cases_dir": where, "taxonomy": {"classes": CLASSES, "escalate": list(ESCALATE)},
            "cases": case_labels(cases)}


def resolve_cases_dir(cases_dir: Path | None = None) -> Path:
    """--cases-dir, else $PACDS_CASES_DIR; there is no default case set."""
    if cases_dir is not None:
        return Path(cases_dir)
    if os.environ.get("PACDS_CASES_DIR"):
        return Path(os.environ["PACDS_CASES_DIR"])
    sys.exit("no case set: pass --cases-dir or set PACDS_CASES_DIR")


def case_labels(cases: list[Case]) -> list[dict[str, str]]:
    """The distinct cases of a run with their labels, for the results file: what was evaluated, against what."""
    unique = {case.id: case for case in cases}
    return [{"id": case.id, "truth": case.truth, "set": case.set, "tier": case.tier} for case in unique.values()]


def build_request(case: Case, presign: Callable[[str], str]) -> dict[str, Any]:
    pacds: dict[str, Any] = {"git": {"url": case.repo, "ref": case.ref}}
    if case.logs:
        pacds["logs"] = [{"name": name, "url": presign(f"replay/{case.id}/{name}")} for name in case.logs]
    return {
        "model": "jev-latest",
        "state": {"pacds": pacds, "user_report": case.report},
        "questions": {"cause": {"type": "choice", "instructions": QUESTION, "criteria": CRITERIA}},
    }


def score(case: Case, response: dict[str, Any], *, tokens: int, seconds: float) -> Result:
    answer = response["answers"]["cause"]
    by_key = {key: letter for letter, key in CLASSES.items()}
    predicted = by_key[answer["choice"]]
    p_truth = answer.get("probabilities", {}).get(CLASSES[case.truth])
    return Result(case_id=case.id, truth=case.truth, predicted=predicted, correct=predicted == case.truth, p_truth=p_truth, tokens=tokens, seconds=seconds)


def summarize(results: list[Result]) -> dict[str, Any]:
    confusion = {truth: {predicted: 0 for predicted in CLASSES} for truth in CLASSES}
    for result in results:
        if result.predicted is not None:
            confusion[result.truth][result.predicted] += 1
    answered = [result.p_truth for result in results if result.p_truth is not None]
    return {
        "cases": len(results),
        "accuracy": sum(result.correct for result in results) / len(results) if results else 0.0,
        "errors": sum(result.error is not None for result in results),
        "mean_p_truth": sum(answered) / len(answered) if answered else None,
        "confusion": confusion,
        "tokens": sum(result.tokens for result in results),
    }


def _replay(case: Case, presign: Callable[[str], str], token: Callable[[], str]) -> Result:
    started = time.monotonic()
    try:
        return _replay_once(case, presign, token, started)
    except Exception as error:  # noqa: BLE001 - report per case, keep going
        seconds = round(time.monotonic() - started, 1)
        return Result(case_id=case.id, truth=case.truth, predicted=None, correct=False, p_truth=None, seconds=seconds, error=repr(error)[:200])


def _replay_once(case: Case, presign: Callable[[str], str], token: Callable[[], str], started: float) -> Result:
    response = httpx.post(f"{BASE_URL}/v1/systemone", json=build_request(case, presign), headers={"Authorization": f"Bearer {token()}"}, timeout=600)
    seconds = round(time.monotonic() - started, 1)
    request_id = response.headers.get(REQUEST_ID_HEADER)
    if response.status_code != 200:
        error = f"{response.status_code} {response.text[:200]}"
        return Result(case_id=case.id, truth=case.truth, predicted=None, correct=False, p_truth=None, seconds=seconds, error=error, request_id=request_id)
    body = response.json()
    tokens = (body.get("usage") or {}).get("input_tokens") or 0
    return replace(score(case, body, tokens=tokens, seconds=seconds), request_id=request_id)


def _run_baseline(cases: list[Case], concurrency: int, cases_dir: Path, trace_dir: Path | None = None,
                  replay_from: str | None = None) -> list[Result]:
    import asyncio
    import uuid

    import openai

    from pacds.engine.trace import Trace
    from pacds_eval.runs import client_recordings, llm_extra_body, llm_structured_outputs, llm_timeout, repeats, write_trace
    from pacds_eval.baseline import evaluate_baseline

    client = None if os.environ.get("LLM_API") == "claude_code" else openai.AsyncOpenAI(
        base_url=os.environ["LLM_BASE_URL"], api_key=os.environ.get("LLM_API_KEY") or "not-needed", max_retries=0, timeout=llm_timeout())
    header = os.environ.get("LLM_SESSION_HEADER")
    semaphore = asyncio.Semaphore(concurrency)
    replay = client_recordings(replay_from)

    async def one(case: Case, repeat: int) -> Result:
        trace = Trace(case_id=case.id, repeat=repeat, variant="baseline", model=os.environ["LLM_MODEL"]) if trace_dir else None
        async with semaphore:
            session = client.with_options(default_headers={header: str(uuid.uuid4())}) if header and client else client
            try:
                result = await evaluate_baseline(case, cases_dir, client=session, model=os.environ["LLM_MODEL"], api=os.environ.get("LLM_API") or "chat_completions",
                                                 max_output_tokens=int(os.environ.get("LLM_MAX_OUTPUT_TOKENS") or 0) or None, trace=trace,
                                                 replay=replay, extra_body=llm_extra_body(), timeout_seconds=llm_timeout(),
                                                 structured_outputs=llm_structured_outputs())
            except Exception as error:  # noqa: BLE001 - report per case, keep going
                result = Result(case_id=case.id, truth=case.truth, predicted=None, correct=False, p_truth=None, error=repr(error)[:200])
        if trace is not None:
            trace.info.update(predicted=result.predicted, p_truth=result.p_truth, error=result.error)
            write_trace(trace_dir, case.id, repeat, trace)
        return replace(result, repeat=repeat)

    async def run() -> list[Result]:
        return list(await asyncio.gather(*(one(case, repeat) for case, repeat in repeats(cases))))

    return asyncio.run(run())


def main() -> None:
    from pacds_eval.s3 import LogStore

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--case", action="append", help="replay only this case id (repeatable)")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--out", type=Path, help="write per-case results and the summary as JSON")
    parser.add_argument("--baseline", action="store_true", help="answer without PACDS: report and logs only, no code")
    parser.add_argument("--set", action="append", choices=SETS, help="replay only this case set (repeatable; default all)")
    parser.add_argument("--repeat", type=int, default=1, help="replay every case N times")
    parser.add_argument("--tier", action="append", help="only cases of this review tier (repeatable), e.g. certain, probable")
    parser.add_argument("--cases-dir", type=Path, help="a case set directory (default $PACDS_CASES_DIR)")
    parser.add_argument("--from-run", type=Path, help="targeted run: pick cases from this run directory or results file (see --select)")
    parser.add_argument("--select", action="append", default=[], help="with --from-run: misses, class=X, tier=X, flipped=RUN, all "
                        "(repeatable, all must hold; python -m pacds_eval.analysis select)")
    parser.add_argument("--no-regression", action="store_true", help="with --from-run: leave out the regression sample")
    parser.add_argument("--replay-from", help="RUN[,RUN...]: answer identical model requests with that run's recorded responses "
                        "(scripts/eval.sh --replay-from also replays PACDS)")
    parser.add_argument("--trace-dir", type=Path, help="--baseline: write one trace per answer as DIR/<case>-<repeat>.json "
                        "(PACDS traces its own requests server-side)")
    args = parser.parse_args()

    cases_dir = resolve_cases_dir(args.cases_dir)
    only, selection = args.case, None
    if args.from_run:
        from pacds_eval.analysis.select import targeted_case_ids

        chosen, selection = targeted_case_ids(args.from_run, args.select, kind="replay", variant="baseline" if args.baseline else "pacds",
                                              cases_dir=cases_dir, regression=not args.no_regression)
        only = sorted(set(chosen) | set(args.case or []))
        print(f"targeted: {len(selection['selected'])} selected from {selection['from_run']}/{selection['evaluation']}, "
              f"{len(selection['regression'])} regression")
    cases = load_cases(cases_dir, only=only, sets=args.set, tiers=args.tier) * args.repeat
    if args.baseline:
        results = _run_baseline(cases, args.concurrency, cases_dir, args.trace_dir, args.replay_from)
    else:
        sign = LogStore.from_env().presign
        bearer = TokenSource().get
        from pacds_eval.runs import repeats

        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            results = list(pool.map(lambda pair: replace(_replay(pair[0], sign, bearer), repeat=pair[1]), repeats(cases)))

    print(f"{'case':20} {'truth':5} {'pred':5} {'p(truth)':>8} {'tokens':>8} {'secs':>6}  error")
    for r in results:
        p = f"{r.p_truth:.2f}" if r.p_truth is not None else "-"
        mark = "ok" if r.correct else "XX"
        print(f"{r.case_id:20} {r.truth:5} {(r.predicted or '-'):5} {p:>8} {r.tokens:>8} {r.seconds:>6}  {mark} {r.error or ''}")
    summary = summarize(results)
    print(f"\naccuracy {summary['accuracy']:.0%} ({sum(r.correct for r in results)}/{len(results)}), errors {summary['errors']}, "
          f"mean p(truth) {summary['mean_p_truth'] or 0:.2f}, input tokens {summary['tokens']}")
    set_of = {case.id: case.set for case in cases}
    tier_of = {case.id: case.tier for case in cases}
    for name in SETS:
        for tier in ("certain", "probable", None):
            group = [r for r in results if set_of[r.case_id] == name and (tier is None or tier_of[r.case_id] == tier)]
            if group:
                label = f"{name}/{tier}" if tier else f"{name} (all)"
                print(f"  {label:16} accuracy {sum(r.correct for r in group) / len(group):.0%} ({sum(r.correct for r in group)}/{len(group)})")
    print("confusion (rows = truth, cols = predicted):")
    print("     " + "  ".join(CLASSES))
    for truth, row in summary["confusion"].items():
        print(f"  {truth}  " + "  ".join(str(row[p]) for p in CLASSES))
    if args.out:
        args.out.write_text(json.dumps({"baseline": args.baseline, **run_description(cases, cases_dir, args.repeat), "selection": selection, "summary": summary, "results": [{**asdict(r), "set": set_of[r.case_id], "tier": tier_of[r.case_id]} for r in results]}, indent=2))


if __name__ == "__main__":
    main()
