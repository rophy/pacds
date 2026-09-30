"""Run the support agent over the replay cases and score its triage decisions.

Usage: python -m pacds_eval.support_agent.run [--variant full|no-pacds] [--set clear|hard] [--case ID] [--repeat N] [--out f.json]
Needs PACDS reachable (PACDS_URL, default the Compose stack; scripts/eval.sh --support), seeded logs, and LLM_* env for the agent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

import httpx
import openai

from pacds.app import REQUEST_ID_HEADER
from pacds.engine.trace import Trace
from pacds_eval.runs import client_recordings, llm_extra_body, llm_timeout, redact, repeats, write_trace
from pacds_eval.oidc import TokenSource
from pacds_eval.harness import BASE_URL, ESCALATE, SETS, Case, load_cases, resolve_cases_dir, run_description
from pacds_eval.support_agent.agent import CLASSES, PACDS_TIMEOUT_SECONDS, Outcome, run_agent

VARIANTS = ("full", "no-pacds")


def score_outcomes(cases: dict[str, Case], outcomes: list[tuple[str, Outcome]]) -> dict[str, Any]:
    confusion = {truth: {predicted: 0 for predicted in CLASSES} for truth in CLASSES}
    for case_id, outcome in outcomes:
        if outcome.decision is not None:
            confusion[cases[case_id].truth][outcome.decision] += 1
    total = len(outcomes) or 1
    class_ok = sum(outcome.decision == cases[case_id].truth for case_id, outcome in outcomes)
    escalation_ok = sum(
        outcome.escalate is not None and outcome.escalate == (cases[case_id].truth in ESCALATE) for case_id, outcome in outcomes
    )
    return {
        "tickets": len(outcomes),
        "class_accuracy": class_ok / total,
        "escalation_accuracy": escalation_ok / total,
        "undecided": sum(outcome.decision is None for _, outcome in outcomes),
        "pacds_calls_per_ticket": sum(len(outcome.pacds_requests) for _, outcome in outcomes) / total,
        "invalid_requests": sum(outcome.invalid_requests for _, outcome in outcomes),
        "pacds_errors": sum(outcome.pacds_errors for _, outcome in outcomes),
        "agent_input_tokens": sum(outcome.input_tokens for _, outcome in outcomes),
        "confusion": confusion,
    }


def _http_pacds(token: Callable[[], str]):
    async def call(case: Case, body: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=PACDS_TIMEOUT_SECONDS) as http:
            response = await http.post(f"{BASE_URL}/v1/systemone", json=body, headers={"Authorization": f"Bearer {token()}"})
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        request_id = response.headers.get(REQUEST_ID_HEADER)
        if response.status_code == 200:
            return {"answers": payload.get("answers", {}), "request_id": request_id}
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        return {"error": {"status": response.status_code, "code": error.get("type"), "message": error.get("message")}, "request_id": request_id}

    return call


async def _run(cases: list[Case], variant: str, concurrency: int, cases_dir: Path,
               trace_dir: Path | None = None,
               on_outcome: Callable[[list[tuple[str, Outcome]]], None] | None = None, replay_from: str | None = None) -> list[tuple[str, Outcome]]:
    from pacds_eval.s3 import LogStore

    store = LogStore.from_env()
    pacds = _http_pacds(TokenSource().get) if variant == "full" else None
    claude_code = os.environ.get("LLM_API") == "claude_code"  # runs the CLI on its own login: no endpoint, no client
    client = None if claude_code else openai.AsyncOpenAI(base_url=os.environ["LLM_BASE_URL"], api_key=os.environ.get("LLM_API_KEY") or "not-needed",
                                                         max_retries=2, timeout=llm_timeout(180))
    header = os.environ.get("LLM_SESSION_HEADER")
    semaphore = asyncio.Semaphore(concurrency)
    finished: list[tuple[str, Outcome]] = []
    replay = client_recordings(replay_from)

    async def one(case: Case, repeat: int) -> tuple[str, Outcome]:
        log_texts = {name: (cases_dir / case.id / name).read_text() for name in case.logs}
        # Signed when the agent attaches the file, so a long run cannot outlive the URL.
        def attachment_url(name: str, case_id: str = case.id) -> str:
            return store.presign(f"replay/{case_id}/{name}")
        session = client.with_options(default_headers={header: str(uuid.uuid4())}) if header and client else client
        trace = Trace(case_id=case.id, repeat=repeat, variant=variant, model=os.environ["LLM_MODEL"]) if trace_dir else None
        async with semaphore:
            try:
                outcome = await run_agent(case, log_texts=log_texts, attachment_url=attachment_url, client=session, model=os.environ["LLM_MODEL"],
                                          pacds=pacds, api=os.environ.get("LLM_API") or "chat_completions",
                                          max_output_tokens=int(os.environ.get("LLM_MAX_OUTPUT_TOKENS") or 0) or None, trace=trace,
                                          replay=replay, extra_body=llm_extra_body(), effort=os.environ.get("LLM_EFFORT") or None,
                                          call_timeout=llm_timeout(180))
            except Exception as error:  # noqa: BLE001 - one failed ticket must not stop the run
                outcome = Outcome(error=repr(error)[:300])
        outcome.repeat = repeat
        if trace is not None:
            trace.info.update(decision=outcome.decision, escalate=outcome.escalate, confidence=outcome.confidence, error=outcome.error)
            write_trace(trace_dir, case.id, repeat, trace)
        if outcome.error:
            print(f"{case.id}: agent failed: {outcome.error}")
        finished.append((case.id, outcome))
        if on_outcome is not None:
            on_outcome(finished)
        return case.id, outcome

    return list(await asyncio.gather(*(one(case, repeat) for case, repeat in repeats(cases))))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--variant", choices=VARIANTS, default="full")
    parser.add_argument("--set", action="append", choices=SETS)
    parser.add_argument("--case", action="append")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--tier", action="append", help="only cases of this review tier (repeatable), e.g. certain, probable")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--from-run", type=Path, help="targeted run: pick cases from this run directory or results file (see --select)")
    parser.add_argument("--select", action="append", default=[], help="with --from-run: misses, class=X, tier=X, flipped=RUN, all "
                        "(repeatable, all must hold; python -m pacds_eval.analysis select)")
    parser.add_argument("--no-regression", action="store_true", help="with --from-run: leave out the regression sample")
    parser.add_argument("--replay-from", help="RUN[,RUN...]: answer identical model requests with that run's recorded responses "
                        "(scripts/eval.sh --replay-from also replays PACDS)")
    parser.add_argument("--trace-dir", type=Path, help="write one trace per ticket (every model call) as DIR/<case>-<repeat>.json")
    parser.add_argument("--cases-dir", type=Path, help="a case set directory (default $PACDS_CASES_DIR)")
    args = parser.parse_args()
    cases_dir = resolve_cases_dir(args.cases_dir)

    only, selection = args.case, None
    if args.from_run:
        from pacds_eval.analysis.select import targeted_case_ids

        chosen, selection = targeted_case_ids(args.from_run, args.select, kind="support", variant=args.variant, cases_dir=cases_dir,
                                              regression=not args.no_regression)
        only = sorted(set(chosen) | set(args.case or []))
        print(f"targeted: {len(selection['selected'])} selected from {selection['from_run']}/{selection['evaluation']}, "
              f"{len(selection['regression'])} regression")
    cases = load_cases(cases_dir, only=only, sets=args.set, tiers=args.tier)
    by_id = {case.id: case for case in cases}

    def write(outcomes: list[tuple[str, Outcome]], complete: bool) -> None:
        # Rewritten after every ticket, so a run that dies midway keeps the decisions made so far.
        if not args.out:
            return
        rows = [{"case_id": i, "set": by_id[i].set, "tier": by_id[i].tier, "truth": by_id[i].truth, **asdict(o)} for i, o in outcomes]
        args.out.write_text(redact(json.dumps({"variant": args.variant, **run_description(cases, cases_dir, args.repeat),
                                               "selection": selection, "complete": complete,
                                               "summary": score_outcomes(by_id, outcomes), "results": rows}, indent=2)))

    outcomes = asyncio.run(_run(cases * args.repeat, args.variant, args.concurrency, cases_dir, args.trace_dir,
                                on_outcome=lambda done: write(done, complete=False), replay_from=args.replay_from))

    print(f"variant={args.variant}")
    print(f"{'case':18} {'set':5} truth  class escalate conf  pacds_calls invalid")
    for case_id, o in sorted(outcomes, key=lambda item: (by_id[item[0]].set, by_id[item[0]].truth, item[0])):
        case = by_id[case_id]
        mark = "ok" if o.decision == case.truth else "XX"
        conf = f"{o.confidence:.2f}" if isinstance(o.confidence, (int, float)) else "-"
        print(f"{case_id:18} {case.set:5} {case.truth:5}  {o.decision or '-':5} {str(o.escalate):8} {conf:5} {len(o.pacds_requests):11} {o.invalid_requests:7}  {mark}")
    for name in (None, *SETS):
        subset = [(i, o) for i, o in outcomes if name is None or by_id[i].set == name]
        if subset:
            s = score_outcomes(by_id, subset)
            print(f"{name or 'all':5}: class {s['class_accuracy']:.0%}  escalation {s['escalation_accuracy']:.0%}  undecided {s['undecided']}  "
                  f"pacds calls/ticket {s['pacds_calls_per_ticket']:.1f}  invalid requests {s['invalid_requests']}  errors {s['pacds_errors']}")
    summary = score_outcomes(by_id, outcomes)
    print("confusion (rows = truth, cols = predicted):\n     " + "  ".join(CLASSES))
    for truth, row in summary["confusion"].items():
        print(f"  {truth}  " + "  ".join(str(row[p]) for p in CLASSES))
    write(outcomes, complete=True)


if __name__ == "__main__":
    main()
