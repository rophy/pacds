"""Failure-mode classification of a run's misses: why did the support agent decide wrongly?

Failed tickets are `infrastructure` and tickets without a PACDS call are `no_pacds`, by rule. Every other miss goes to a
reviewer model (LLM_* from the environment) with the checked-in prompt (prompts/classify.md) and a dossier: the label
and reviewed fix, the agent's conversation, and what each PACDS call investigated and answered. Results are cached in
<report dir>/classify/<evaluation>/<case>-<repeat>.json per prompt hash, so a report never pays for them twice.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

from pacds.engine import responses_api
from pacds.engine.trace import sha256
from tests.analysis.load import Attempt, Evaluation, Run, failed

PROMPT_FILE = Path(__file__).parent / "prompts" / "classify.md"
MODES = ("no_pacds", "wrong_questions", "pacds_wrong", "pacds_wrong_evidence", "agent_overrode", "label_debatable", "infrastructure")
LLM_MODES = MODES[1:6]
SCHEMA = {
    "type": "object",
    "properties": {"mode": {"type": "string", "enum": list(LLM_MODES)}, "deciding_fact": {"type": "string"}, "explanation": {"type": "string"}},
    "required": ["mode", "deciding_fact", "explanation"],
    "additionalProperties": False,
}
MAX_MESSAGE_CHARS = 4000


def prompt() -> str:
    return PROMPT_FILE.read_text()


def cache_path(root: Path, evaluation: Evaluation, attempt: Attempt) -> Path:
    return root / "classify" / evaluation.name / f"{attempt.case_id}-{attempt.repeat}.json"


def misses(evaluation: Evaluation) -> list[Attempt]:
    return [a for a in evaluation.attempts if not a.correct]


def by_rule(evaluation: Evaluation, attempt: Attempt) -> str | None:
    if failed(attempt):
        return "infrastructure"
    if evaluation.variant != "no-pacds" and not attempt.row.get("pacds_requests"):
        return "no_pacds"
    return None


def _clip(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    return text if len(text) <= limit else f"{text[:limit]} …[{len(text) - limit} characters cut]"


def dossier(run: Run, evaluation: Evaluation, attempt: Attempt) -> str:
    classes = evaluation.taxonomy["classes"]
    case = evaluation.case_file(attempt.case_id) or {}
    fixes = [review.get("fix") for review in case.get("reviews", []) if review.get("fix")]
    conversation = []
    for message in attempt.row.get("transcript", []):
        entry = {"role": message.get("role"), "content": _clip(message.get("content") or "")}
        if message.get("tool_calls"):
            entry["tool_calls"] = [{"name": c["function"]["name"], "arguments": _clip(c["function"]["arguments"])} for c in message["tool_calls"]]
        conversation.append(entry)
    investigations = []
    for trace in run.traces_for(attempt):
        tools = trace.get("tools", [])
        investigations.append({
            "request_id": trace.get("request_id"),
            "questions": trace.get("questions"),
            "answers": (trace.get("final") or {}).get("validated"),
            "turns": (trace.get("investigation") or {}).get("turns"),
            "tool_calls": [{"name": t["name"], "arguments": _clip(t.get("arguments") or "", 300), "result_chars": t.get("result_chars")} for t in tools],
            "used_history": any(t["name"] in ("git_log", "git_show") for t in tools),
        })
    return json.dumps({
        "true_class": f"{attempt.truth} ({classes.get(attempt.truth)})",
        "agent_decision": f"{attempt.decision} ({classes.get(attempt.decision or '')})",
        "classes": classes,
        "reviewed_fix": fixes,
        "conversation": conversation,
        "pacds_investigations": investigations,
    }, indent=1)


async def _ask(client: Any, model: str, api: str, text: str) -> tuple[dict[str, Any], dict[str, int]]:
    messages = [{"role": "system", "content": prompt()}, {"role": "user", "content": text}]
    response_format = {"type": "json_schema", "json_schema": {"name": "classification", "schema": SCHEMA, "strict": True}}
    if api == "responses":
        response = responses_api.from_response(await client.responses.create(model=model, **responses_api.request_kwargs(
            messages=messages, response_format=response_format)))
    else:
        response = await client.chat.completions.create(model=model, messages=messages, response_format=response_format)
    usage = {"input": response.usage.prompt_tokens, "output": response.usage.completion_tokens} if response.usage else {}
    return json.loads(response.choices[0].message.content or "{}"), usage


def cached(root: Path, evaluation: Evaluation, attempt: Attempt) -> dict[str, Any] | None:
    path = cache_path(root, evaluation, attempt)
    if not path.is_file():
        return None
    data = json.loads(path.read_text())
    return data if data.get("prompt_sha256") in (None, sha256(prompt())) else None


def classify(run: Run, evaluations: list[Evaluation], root: Path, *, concurrency: int = 4, limit: int | None = None,
             dry_run: bool = False) -> list[dict[str, Any]]:
    """Classify every miss not yet classified under the current prompt; return all classifications."""
    todo, results = [], []
    for evaluation in evaluations:
        for attempt in misses(evaluation):
            hit = cached(root, evaluation, attempt)
            if hit is not None:
                results.append(hit)
                continue
            mode = by_rule(evaluation, attempt)
            if mode is not None:
                results.append(_store(root, evaluation, attempt, {"mode": mode, "by": "rule"}))
            else:
                todo.append((evaluation, attempt))
    if limit is not None:
        todo = todo[:limit]
    if dry_run:
        sizes = [len(dossier(run, e, a)) for e, a in todo]
        print(f"{len(todo)} misses to classify, about {sum(sizes) // 4:,} input tokens")
        return results
    if todo:
        import openai

        client = openai.AsyncOpenAI(base_url=os.environ["LLM_BASE_URL"], api_key=os.environ.get("LLM_API_KEY") or "not-needed",
                                    max_retries=2, timeout=180)
        model, api = os.environ["LLM_MODEL"], os.environ.get("LLM_API") or "chat_completions"
        semaphore = asyncio.Semaphore(concurrency)

        async def one(evaluation: Evaluation, attempt: Attempt) -> dict[str, Any]:
            async with semaphore:
                try:
                    answer, usage = await _ask(client, model, api, dossier(run, evaluation, attempt))
                except Exception as error:  # noqa: BLE001 - keep going; an unclassified miss is retried next time
                    return {"case_id": attempt.case_id, "repeat": attempt.repeat, "evaluation": evaluation.name, "error": repr(error)[:200]}
            if answer.get("mode") not in LLM_MODES:
                return {"case_id": attempt.case_id, "repeat": attempt.repeat, "evaluation": evaluation.name, "error": f"bad answer {answer}"}
            return _store(root, evaluation, attempt, {**answer, "by": model, "usage": usage})

        async def everything() -> list[dict[str, Any]]:
            return list(await asyncio.gather(*(one(e, a) for e, a in todo)))

        results += asyncio.run(everything())
    return results


def _store(root: Path, evaluation: Evaluation, attempt: Attempt, result: dict[str, Any]) -> dict[str, Any]:
    record = {"evaluation": evaluation.name, "case_id": attempt.case_id, "repeat": attempt.repeat, "truth": attempt.truth,
              "decision": attempt.decision, "prompt_sha256": sha256(prompt()), **result}
    path = cache_path(root, evaluation, attempt)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n")
    return record


def load_classifications(root: Path, evaluation: Evaluation) -> dict[tuple[str, int], dict[str, Any]]:
    """Cached classifications under the current prompt, by (case, repeat)."""
    found = {}
    for attempt in misses(evaluation):
        hit = cached(root, evaluation, attempt)
        if hit is not None:
            found[(attempt.case_id, attempt.repeat)] = hit
    return found


def summary(evaluation: Evaluation, classifications: dict[tuple[str, int], dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Misses by true class and mode; 'unclassified' for misses without a result yet."""
    table: dict[str, Counter[str]] = {}
    for attempt in misses(evaluation):
        mode = (classifications.get((attempt.case_id, attempt.repeat)) or {}).get("mode", "unclassified")
        table.setdefault(attempt.truth, Counter())[mode] += 1
    return {truth: dict(counts.most_common()) for truth, counts in sorted(table.items())}
