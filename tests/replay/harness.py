"""Replay real GitHub support cases through PACDS and score its verdicts (see tests/replay/cases).

Usage: uv run python -m tests.replay.harness [--case ID ...] [--concurrency N] [--out results.json] [--baseline]
Needs the Kind dev cluster, PACDS port-forwarded to localhost:3002 and ./scripts/seed-logs.sh run once.
--baseline skips PACDS: the same model answers from the report and logs only, using LLM_* from the
environment (e.g. `set -a; . ./.env; set +a`).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

CASES_DIR = Path(__file__).parent / "cases"
BASE_URL = os.environ.get("PACDS_URL", "http://localhost:3002")
CONTEXT = "kind-pacds"

# Ground-truth classes (A-D) and the option key PACDS answers with for each.
CLASSES = {"A": "other_system", "B": "user_error", "C": "infrastructure", "D": "bug"}
CRITERIA = {
    "other_system": "Not caused by this application: an upstream library or service, the user's browser or OS, or another product.",
    "user_error": "The application works as designed, even if the user did not expect the behavior; the user misused it, "
    "misunderstood a feature, or entered a wrong setting or input.",
    "infrastructure": "The application code is fine, but the environment it is deployed in is misconfigured or failing: "
    "reverse proxy, database, container or network, storage, file permissions, or server configuration.",
    "bug": "A defect in this application's own code.",
}
QUESTION = "What caused the problem described in the user's report?"
# clear: the report alone mostly decides the class; hard: only the code does (the no-code baseline fails).
SETS = ("clear", "hard")


@dataclass(frozen=True)
class Case:
    id: str
    truth: str
    repo: str
    ref: str
    report: str
    logs: list[str] = field(default_factory=list)
    set: str = "clear"


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


def load_cases(root: Path, only: list[str] | None = None, sets: list[str] | None = None) -> list[Case]:
    cases = []
    for path in sorted(root.glob("*/case.json")):
        data = json.loads(path.read_text())
        for name in data.get("logs", []):
            if not (path.parent / name).is_file():
                raise ValueError(f"{data['id']}: log file {name} is missing")
        case_set = data.get("set", "clear")
        if case_set not in SETS:
            raise ValueError(f"{data['id']}: unknown set {case_set!r}")
        cases.append(Case(id=data["id"], truth=data["truth"], repo=data["repo"], ref=data["ref"], report=data["report"],
                          logs=list(data.get("logs", [])), set=case_set))
    if sets:
        cases = [case for case in cases if case.set in sets]
    if only:
        unknown = sorted(set(only) - {case.id for case in cases})
        if unknown:
            raise ValueError(f"unknown case ids: {', '.join(unknown)}")
        cases = [case for case in cases if case.id in only]
    return cases


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


def _token() -> str:
    return subprocess.run(
        ["kubectl", "--context", CONTEXT, "-n", "support", "create", "token", "triage-agent", "--audience", "pacds"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _replay(case: Case, presign: Callable[[str], str], token: str) -> Result:
    started = time.monotonic()
    response = httpx.post(f"{BASE_URL}/v1/systemone", json=build_request(case, presign), headers={"Authorization": f"Bearer {token}"}, timeout=600)
    seconds = round(time.monotonic() - started, 1)
    if response.status_code != 200:
        error = f"{response.status_code} {response.text[:200]}"
        return Result(case_id=case.id, truth=case.truth, predicted=None, correct=False, p_truth=None, seconds=seconds, error=error)
    body = response.json()
    tokens = (body.get("usage") or {}).get("input_tokens") or 0
    return score(case, body, tokens=tokens, seconds=seconds)


def _run_baseline(cases: list[Case], concurrency: int) -> list[Result]:
    import asyncio
    import uuid

    import openai

    from tests.replay.baseline import evaluate_baseline

    client = openai.AsyncOpenAI(base_url=os.environ["LLM_BASE_URL"], api_key=os.environ["LLM_API_KEY"], max_retries=0, timeout=120)
    header = os.environ.get("LLM_SESSION_HEADER")
    semaphore = asyncio.Semaphore(concurrency)

    async def one(case: Case) -> Result:
        async with semaphore:
            session = client.with_options(default_headers={header: str(uuid.uuid4())}) if header else client
            try:
                return await evaluate_baseline(case, CASES_DIR, client=session, model=os.environ["LLM_MODEL"], api=os.environ.get("LLM_API") or "chat_completions")
            except Exception as error:  # noqa: BLE001 - report per case, keep going
                return Result(case_id=case.id, truth=case.truth, predicted=None, correct=False, p_truth=None, error=repr(error)[:200])

    async def run() -> list[Result]:
        return list(await asyncio.gather(*(one(case) for case in cases)))

    return asyncio.run(run())


def main() -> None:
    from tests.s3 import dev_credentials, presign

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--case", action="append", help="replay only this case id (repeatable)")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--out", type=Path, help="write per-case results and the summary as JSON")
    parser.add_argument("--baseline", action="store_true", help="answer without PACDS: report and logs only, no code")
    parser.add_argument("--set", action="append", choices=SETS, help="replay only this case set (repeatable; default all)")
    parser.add_argument("--repeat", type=int, default=1, help="replay every case N times")
    args = parser.parse_args()

    cases = load_cases(CASES_DIR, only=args.case, sets=args.set) * args.repeat
    if args.baseline:
        results = _run_baseline(cases, args.concurrency)
    else:
        access_key, secret_key = dev_credentials(CONTEXT)
        sign = lambda key: presign(key, access_key=access_key, secret_key=secret_key)  # noqa: E731
        token = _token()
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            results = list(pool.map(lambda case: _replay(case, sign, token), cases))

    print(f"{'case':20} {'truth':5} {'pred':5} {'p(truth)':>8} {'tokens':>8} {'secs':>6}  error")
    for r in results:
        p = f"{r.p_truth:.2f}" if r.p_truth is not None else "-"
        mark = "ok" if r.correct else "XX"
        print(f"{r.case_id:20} {r.truth:5} {(r.predicted or '-'):5} {p:>8} {r.tokens:>8} {r.seconds:>6}  {mark} {r.error or ''}")
    summary = summarize(results)
    print(f"\naccuracy {summary['accuracy']:.0%} ({sum(r.correct for r in results)}/{len(results)}), errors {summary['errors']}, "
          f"mean p(truth) {summary['mean_p_truth'] or 0:.2f}, input tokens {summary['tokens']}")
    set_of = {case.id: case.set for case in cases}
    for name in SETS:
        in_set = [r for r in results if set_of[r.case_id] == name]
        if in_set:
            print(f"  {name:5} accuracy {sum(r.correct for r in in_set) / len(in_set):.0%} ({sum(r.correct for r in in_set)}/{len(in_set)})")
    print("confusion (rows = truth, cols = predicted):")
    print("     " + "  ".join(CLASSES))
    for truth, row in summary["confusion"].items():
        print(f"  {truth}  " + "  ".join(str(row[p]) for p in CLASSES))
    if args.out:
        args.out.write_text(json.dumps({"summary": summary, "results": [{**asdict(r), "set": set_of[r.case_id]} for r in results]}, indent=2))


if __name__ == "__main__":
    main()
