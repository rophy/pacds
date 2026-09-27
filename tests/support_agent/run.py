"""Run the support agent over the replay cases and score its triage decisions.

Usage: python -m tests.support_agent.run [--variant full|no-pacds] [--set clear|hard] [--case ID] [--repeat N] [--out f.json]
Needs PACDS reachable (PACDS_URL, default the Compose stack; scripts/eval.sh --support), seeded logs, and LLM_* env for the agent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx
import openai

from pacds.app import REQUEST_ID_HEADER
from tests.oidc import token
from tests.replay.harness import BASE_URL, CASES_DIR, SETS, Case, load_cases
from tests.support_agent.agent import CLASSES, Outcome, run_agent

VARIANTS = ("full", "no-pacds")
# Presigned signatures are credentials; the transcripts in --out must not carry them.
SIGNATURE = re.compile(r"(X-Amz-Signature=)[^&\s\"\\]+")


def redact(text: str) -> str:
    return SIGNATURE.sub(r"\1REDACTED", text)


def score_outcomes(cases: dict[str, Case], outcomes: list[tuple[str, Outcome]]) -> dict[str, Any]:
    confusion = {truth: {predicted: 0 for predicted in CLASSES} for truth in CLASSES}
    for case_id, outcome in outcomes:
        if outcome.decision is not None:
            confusion[cases[case_id].truth][outcome.decision] += 1
    total = len(outcomes) or 1
    class_ok = sum(outcome.decision == cases[case_id].truth for case_id, outcome in outcomes)
    escalation_ok = sum(
        outcome.escalate is not None and outcome.escalate == (cases[case_id].truth == "D") for case_id, outcome in outcomes
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


def _http_pacds(token: str):
    async def call(case: Case, body: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=600) as http:
            response = await http.post(f"{BASE_URL}/v1/systemone", json=body, headers={"Authorization": f"Bearer {token}"})
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


async def _run(cases: list[Case], variant: str, concurrency: int) -> list[tuple[str, Outcome]]:
    from tests.s3 import dev_credentials, presign

    access_key, secret_key = dev_credentials()
    pacds = _http_pacds(token()) if variant == "full" else None
    client = openai.AsyncOpenAI(base_url=os.environ["LLM_BASE_URL"], api_key=os.environ["LLM_API_KEY"], max_retries=1, timeout=180)
    header = os.environ.get("LLM_SESSION_HEADER")
    semaphore = asyncio.Semaphore(concurrency)

    async def one(case: Case) -> tuple[str, Outcome]:
        log_texts = {name: (CASES_DIR / case.id / name).read_text() for name in case.logs}
        log_urls = [{"name": name, "url": presign(f"replay/{case.id}/{name}", access_key=access_key, secret_key=secret_key)} for name in case.logs]
        session = client.with_options(default_headers={header: str(uuid.uuid4())}) if header else client
        async with semaphore:
            try:
                outcome = await run_agent(case, log_texts=log_texts, log_urls=log_urls, client=session, model=os.environ["LLM_MODEL"],
                                          pacds=pacds, api=os.environ.get("LLM_API") or "chat_completions")
            except Exception as error:  # noqa: BLE001 - one failed ticket must not stop the run
                outcome = Outcome(error=repr(error)[:300])
        if outcome.error:
            print(f"{case.id}: agent failed: {outcome.error}")
        return case.id, outcome

    return list(await asyncio.gather(*(one(case) for case in cases)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--variant", choices=VARIANTS, default="full")
    parser.add_argument("--set", action="append", choices=SETS)
    parser.add_argument("--case", action="append")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    cases = load_cases(CASES_DIR, only=args.case, sets=args.set)
    by_id = {case.id: case for case in cases}
    outcomes = asyncio.run(_run(cases * args.repeat, args.variant, args.concurrency))

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
    if args.out:
        rows = [{"case_id": i, "set": by_id[i].set, "tier": by_id[i].tier, "truth": by_id[i].truth, **asdict(o)} for i, o in outcomes]
        args.out.write_text(redact(json.dumps({"variant": args.variant, "summary": summary, "results": rows}, indent=2)))


if __name__ == "__main__":
    main()
