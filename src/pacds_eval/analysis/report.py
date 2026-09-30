"""Build a run's report from its results files and traces: outcomes, per-case records, cost, reliability, behavior.

Writes RUN/report/report.md (for people), cases.jsonl (one line per evaluation and case) and costs.json.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from pacds_eval.analysis.checks import QUESTION_CHECKS, matched_checks
from pacds_eval.analysis.load import Attempt, Evaluation, Run
from pacds_eval.analysis.stats import median, wilson

HISTORY_TOOLS = {"git_log", "git_show"}
NO_DECISION = "-"


# --- outcomes ---------------------------------------------------------------------------------------------------

def _rate(successes: int, n: int) -> dict[str, Any]:
    low, high = wilson(successes, n)
    return {"n": n, "correct": successes, "accuracy": successes / n if n else None, "ci95": [round(low, 3), round(high, 3)]}


def case_majority(attempts: list[Attempt]) -> bool:
    """Right in more than half of its repeats (2 of 3)."""
    return sum(a.correct for a in attempts) * 2 > len(attempts)


def outcomes(evaluation: Evaluation) -> dict[str, Any]:
    attempts = evaluation.attempts
    classes = list(evaluation.taxonomy["classes"])
    escalate = set(evaluation.taxonomy.get("escalate", []))
    by_class = {c: _rate(sum(a.correct for a in attempts if a.truth == c), sum(a.truth == c for a in attempts)) for c in classes}
    tiers = sorted({str(evaluation.cases.get(a.case_id, {}).get("tier")) for a in attempts})
    by_tier = {t: _rate(sum(a.correct for a in attempts if str(evaluation.cases.get(a.case_id, {}).get("tier")) == t),
                        sum(str(evaluation.cases.get(a.case_id, {}).get("tier")) == t for a in attempts)) for t in tiers}
    confusion = {truth: {predicted: 0 for predicted in [*classes, NO_DECISION]} for truth in classes}
    for attempt in attempts:
        if attempt.truth in confusion:
            confusion[attempt.truth][attempt.decision if attempt.decision in confusion[attempt.truth] else NO_DECISION] += 1
    grouped = evaluation.by_case()
    per_case = {case_id: {"truth": group[0].truth, "right": sum(a.correct for a in group), "repeats": len(group),
                          "majority": case_majority(group), "decisions": [a.decision for a in sorted(group, key=lambda a: a.repeat)]}
                for case_id, group in sorted(grouped.items())}
    escalations = [a for a in attempts if a.escalate is not None]
    result: dict[str, Any] = {
        "overall": _rate(sum(a.correct for a in attempts), len(attempts)),
        "by_class": by_class,
        "by_tier": by_tier,
        "cases": _rate(sum(c["majority"] for c in per_case.values()), len(per_case)),
        "per_case": per_case,
        "confusion": confusion,
        "undecided": sum(a.decision is None for a in attempts),
        "errors": sum(bool(a.error) for a in attempts),
    }
    if evaluation.kind == "support":
        result["escalation"] = _rate(sum(a.escalate == (a.truth in escalate) for a in escalations), len(escalations))
    return result


# --- PACDS requests and cost ------------------------------------------------------------------------------------

def _chars(message: dict[str, Any]) -> int:
    calls = message.get("tool_calls")
    return len(message.get("content") or "") + (len(json.dumps(calls)) if calls else 0)


def _source(message: dict[str, Any], index: int, tool_names: dict[str, str]) -> str:
    role = message.get("role")
    if role == "tool":
        return f"tool:{tool_names.get(message.get('tool_call_id', ''), '?')}"
    if role == "user":
        # PACDS and the baseline send the questions, then the document; later user messages are instructions.
        return "questions" if index == 1 else "document+instructions"
    return str(role)


def input_attribution(calls: list[dict[str, Any]]) -> dict[str, Any]:
    """Split each call's input tokens over the messages it sent, in proportion to their size.

    Tokens for tool definitions and message framing are spread the same way. 'resent' is input that had already been
    sent in the previous call of the same conversation: the price of a stateless API, unless the provider caches it.
    """
    by_source: Counter[str] = Counter()
    resent = resent_tools = 0.0
    messages: list[dict[str, Any]] = []
    tool_names: dict[str, str] = {}
    for position, call in enumerate(calls):
        messages = messages[: call["kept"]] + call["messages_added"]
        for message in call["messages_added"]:
            for tool_call in message.get("tool_calls") or []:
                tool_names[tool_call["id"]] = tool_call.get("name") or tool_call["function"]["name"]  # session traces store calls flat
        usage = call.get("usage")
        if not usage:
            continue
        sizes = [_chars(m) for m in messages]
        ratio = usage["input"] / (sum(sizes) or 1)
        for index, (message, size) in enumerate(zip(messages, sizes)):
            tokens = size * ratio
            by_source[_source(message, index, tool_names)] += tokens
            if position > 0 and index < call["kept"]:
                resent += tokens
                if message.get("role") == "tool":
                    resent_tools += tokens
    return {"by_source": {k: round(v) for k, v in by_source.most_common() if round(v)}, "resent": round(resent), "resent_tool_results": round(resent_tools)}


def request_summary(trace: dict[str, Any]) -> dict[str, Any]:
    tools = trace.get("tools", [])
    files = sorted({json.loads(t["arguments"] or "{}").get("path", "") for t in tools if t["name"] == "read_file" and _is_json(t["arguments"])})
    return {
        "request_id": trace.get("request_id"),
        "status": trace.get("status"),
        "error": trace.get("error"),
        "questions": {qid: q.get("instructions") for qid, q in (trace.get("questions") or {}).items()},
        "answers": (trace.get("final") or {}).get("validated"),
        "turns": (trace.get("investigation") or {}).get("turns"),
        "ended": (trace.get("investigation") or {}).get("reason"),
        "tools": dict(Counter(t["name"] for t in tools)),
        "files_read": files,
        "history": any(t["name"] in HISTORY_TOOLS for t in tools),
        "tokens": trace.get("usage"),
    }


def _is_json(text: str | None) -> bool:
    try:
        return isinstance(json.loads(text or "{}"), dict)
    except json.JSONDecodeError:
        return False


def _add(total: dict[str, int], usage: dict[str, int] | None) -> None:
    for key in ("input", "output", "cached", "reasoning"):
        total[key] = total.get(key, 0) + int((usage or {}).get(key, 0))


def costs(run: Run, evaluation: Evaluation, correct: int) -> dict[str, Any]:
    pacds: dict[str, int] = {}
    by_phase: dict[str, dict[str, int]] = {}
    by_source: Counter[str] = Counter()
    resent = resent_tools = 0
    requests = 0
    for attempt in evaluation.attempts:
        for trace in run.traces_for(attempt):
            requests += 1
            _add(pacds, trace.get("usage"))
            for call in trace.get("calls", []):
                _add(by_phase.setdefault(call["phase"], {}), call.get("usage"))
            attribution = input_attribution(trace.get("calls", []))
            by_source.update(attribution["by_source"])
            resent += attribution["resent"]
            resent_tools += attribution["resent_tool_results"]
    calls = {"pacds": {"live": 0, "replayed": 0}, "client": {"live": 0, "replayed": 0}}
    live_input = 0
    for attempt in evaluation.attempts:
        for component, traces in (("pacds", run.traces_for(attempt)), ("client", [attempt.client_trace] if attempt.client_trace else [])):
            for trace in traces:
                for call in trace.get("calls", []):
                    replayed = bool(call.get("replayed"))
                    calls[component]["replayed" if replayed else "live"] += 1
                    live_input += 0 if replayed else (call.get("usage") or {}).get("input", 0)
    client: dict[str, int] = {}
    for attempt in evaluation.attempts:
        if attempt.client_trace is not None:
            _add(client, attempt.client_trace.get("usage"))
        else:  # no client trace: the results row's own count (input only)
            _add(client, {"input": attempt.row.get("input_tokens") if evaluation.kind == "support" else
                          (attempt.row.get("tokens") if evaluation.variant == "baseline" else 0)})
    total_input = pacds.get("input", 0) + client.get("input", 0)
    attempts = len(evaluation.attempts)
    return {
        "client": client,
        "pacds": pacds,
        "pacds_requests": requests,
        "pacds_by_phase": by_phase,
        "pacds_input_by_source": dict(by_source.most_common()),
        "pacds_resent_share": resent / pacds["input"] if pacds.get("input") else None,
        "pacds_resent_tool_results_share": resent_tools / pacds["input"] if pacds.get("input") else None,
        "pacds_cached_share": pacds.get("cached", 0) / pacds["input"] if pacds.get("input") else None,
        "input_total": total_input,
        "calls": calls,
        "live_input": live_input,
        "input_per_attempt": total_input / attempts if attempts else None,
        "input_per_correct": total_input / correct if correct else None,
    }


# --- reliability and behavior ------------------------------------------------------------------------------------

def reliability(run: Run, evaluation: Evaluation) -> dict[str, Any]:
    errors = Counter((a.error or "")[:80] for a in evaluation.attempts if a.error)
    status, codes, ended, finish, retried = Counter(), Counter(), Counter(), Counter(), Counter()
    for attempt in evaluation.attempts:
        traces = run.traces_for(attempt) + ([attempt.client_trace] if attempt.client_trace else [])
        for trace in traces:
            if "status" in trace:
                status[str(trace["status"])] += 1
            if trace.get("error"):
                codes[str(trace["error"])[:80]] += 1
            if trace.get("investigation"):
                ended[trace["investigation"]["reason"]] += 1
            for call in trace.get("calls", []):
                finish[str((call.get("response") or {}).get("finish_reason"))] += 1
                for failed in call.get("attempts", [])[:-1] if call.get("response") else call.get("attempts", []):
                    retried[str(failed.get("status"))] += 1
    return {"attempt_errors": dict(errors), "pacds_status": dict(status), "trace_errors": dict(codes),
            "investigation_ended": dict(ended), "finish_reasons": dict(finish), "failed_model_calls": dict(retried)}


def behavior(run: Run, evaluation: Evaluation) -> dict[str, Any]:
    result: dict[str, Any] = {}
    attempts = evaluation.attempts
    if evaluation.kind == "support" and evaluation.variant != "no-pacds":
        asked = {name: [a for a in attempts if any(name in matched_checks(r.get("questions")) for r in a.row.get("pacds_requests", []))]
                 for name in QUESTION_CHECKS}
        result["pacds_calls_per_ticket"] = sum(len(a.row.get("pacds_requests", [])) for a in attempts) / len(attempts) if attempts else None
        result["questions_per_ticket"] = (sum(len(r.get("questions") or {}) for a in attempts for r in a.row.get("pacds_requests", []))
                                          / len(attempts) if attempts else None)
        result["checks"] = {
            name: {"tickets": len(hits), "share": len(hits) / len(attempts) if attempts else None,
                   "accuracy_when_asked": _rate(sum(a.correct for a in hits), len(hits))["accuracy"],
                   "accuracy_when_not": _rate(sum(a.correct for a in attempts if a not in hits), len(attempts) - len(hits))["accuracy"],
                   "by_class": {c: sum(a.truth == c for a in hits) for c in evaluation.taxonomy["classes"]}}
            for name, hits in asked.items()
        }
    summaries = [request_summary(t) for a in attempts for t in run.traces_for(a)]
    if summaries:
        tools: Counter[str] = Counter()
        for summary in summaries:
            tools.update(summary["tools"])
        result["pacds"] = {
            "requests": len(summaries),
            "turns_median": median([s["turns"] for s in summaries if s["turns"] is not None]),
            "turns_mean": _mean([s["turns"] for s in summaries if s["turns"] is not None]),
            "files_read_median": median([len(s["files_read"]) for s in summaries]),
            "history_share": sum(s["history"] for s in summaries) / len(summaries),
            "tools": dict(tools.most_common()),
        }
    return result


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


# --- per-case records --------------------------------------------------------------------------------------------

def case_records(run: Run, evaluation: Evaluation) -> list[dict[str, Any]]:
    records = []
    for case_id, group in sorted(evaluation.by_case().items()):
        label = evaluation.cases.get(case_id, {})
        case_file = evaluation.case_file(case_id) or {}
        fixes = [review.get("fix") for review in case_file.get("reviews", []) if review.get("fix")]
        records.append({
            "evaluation": evaluation.name, "case_id": case_id, "truth": group[0].truth, "set": label.get("set"), "tier": label.get("tier"),
            "reviewed_fix": fixes, "right": sum(a.correct for a in group), "repeats": len(group), "majority": case_majority(group),
            "attempts": [{
                "repeat": a.repeat, "decision": a.decision, "correct": a.correct, "escalate": a.escalate, "confidence": a.confidence,
                "error": a.error, "client_tokens": (a.client_trace or {}).get("usage"),
                "pacds": [request_summary(t) for t in run.traces_for(a)],
                "pacds_missing_traces": [rid for rid in a.request_ids if rid not in run.pacds_traces],
            } for a in sorted(group, key=lambda a: a.repeat)],
        })
    return records


# --- the report --------------------------------------------------------------------------------------------------

def build(run: Run, root: Path | None = None) -> dict[str, Any]:
    """The report; with root (the report directory), failure modes cached there by classify are included."""
    from pacds_eval.analysis.classify import load_classifications, summary

    linked = {rid for e in run.evaluations for a in e.attempts for rid in a.request_ids}
    evaluations = []
    for evaluation in run.evaluations:
        result = outcomes(evaluation)
        evaluations.append({
            "name": evaluation.name, "kind": evaluation.kind, "variant": evaluation.variant, "repeat": evaluation.repeat,
            "cases": len(evaluation.cases), "outcomes": result,
            "costs": costs(run, evaluation, result["overall"]["correct"]),
            "reliability": reliability(run, evaluation), "behavior": behavior(run, evaluation),
        })
        if root is not None and evaluation.kind == "support":
            found = load_classifications(root, evaluation)
            if found:
                evaluations[-1]["failure_modes"] = summary(evaluation, found)
    return {
        "run": run.name,
        "runs": run.info.get("runs", [run.name]),
        "info": {key: run.info.get(key) for key in ("started", "finished", "commit", "dirty", "llm", "args", "toolkit_version", "target_version")},
        "pacds_llm": {key: ((run.info.get("pacds_config") or {}).get("llm") or {}).get(key) for key in ("model", "base_url")},
        "pacds_traces": len(run.pacds_traces),
        "pacds_traces_unlinked": sorted(set(run.pacds_traces) - linked),
        "evaluations": evaluations,
    }


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def _rate_cell(rate: dict[str, Any]) -> str:
    if not rate["n"]:
        return "-"
    low, high = rate["ci95"]
    return f"{_pct(rate['accuracy'])} ({rate['correct']}/{rate['n']}; {low:.0%}–{high:.0%})"


def _table(header: list[str], rows: list[list[Any]]) -> list[str]:
    return ["| " + " | ".join(header) + " |", "|" + "---|" * len(header), *("| " + " | ".join(str(c) for c in row) + " |" for row in rows)]


def render(report: dict[str, Any]) -> str:
    info = report["info"]
    llm = info.get("llm") or {}
    lines = [f"# Evaluation report: {report['run']}", "",
             f"- Started {info.get('started')}, commit `{(info.get('commit') or '')[:12]}`{' (dirty)' if info.get('dirty') else ''}, "
             f"model `{llm.get('model')}` ({llm.get('api') or 'chat_completions'})"
             + (f", PACDS model `{report['pacds_llm']['model']}`" if (report.get("pacds_llm") or {}).get("model") else ""),
             f"- Versions: toolkit {info.get('toolkit_version') or 'unknown'}, target {info.get('target_version') or 'unknown'}",
             f"- PACDS traces: {report['pacds_traces']} ({len(report['pacds_traces_unlinked'])} not linked to a results row)",
             "- Accuracy cells: rate (right/total; 95% Wilson interval). A case counts as right when right in more than half its repeats.",
             ""]
    for e in report["evaluations"]:
        o, c, r, b = e["outcomes"], e["costs"], e["reliability"], e["behavior"]
        lines += [f"## {e['name']}: {e['kind']} {e['variant']} ({e['cases']} cases × {e['repeat']})", "", "### Outcomes", ""]
        rows = [["all", _rate_cell(o["overall"])], *([f"class {k}", _rate_cell(v)] for k, v in o["by_class"].items() if v["n"]),
                *([f"tier {k}", _rate_cell(v)] for k, v in o["by_tier"].items()), ["cases (majority)", _rate_cell(o["cases"])]]
        if "escalation" in o:
            rows.append(["escalation", _rate_cell(o["escalation"])])
        lines += _table(["group", "accuracy"], rows)
        lines += ["", f"Undecided {o['undecided']}, errors {o['errors']}.", "", "Confusion (rows truth, columns decision):", ""]
        columns = list(next(iter(o["confusion"].values())).keys()) if o["confusion"] else []
        lines += _table(["truth", *columns], [[t, *row.values()] for t, row in o["confusion"].items() if sum(row.values())])
        lines += ["", "Per case:", ""]
        lines += _table(["case", "truth", "right", "decisions"],
                        [[k, v["truth"], f"{v['right']}/{v['repeats']}", " ".join(d or NO_DECISION for d in v["decisions"])]
                         for k, v in o["per_case"].items()])
        lines += ["", "### Cost (input tokens)", ""]
        cost_rows = [["client (agent or baseline)", f"{c['client'].get('input', 0):,}", f"{c['client'].get('cached', 0):,}", f"{c['client'].get('output', 0):,}"]]
        for phase, usage in c["pacds_by_phase"].items():
            cost_rows.append([f"PACDS {phase}", f"{usage.get('input', 0):,}", f"{usage.get('cached', 0):,}", f"{usage.get('output', 0):,}"])
        lines += _table(["component", "input", "cached", "output"], cost_rows)
        per_correct = f"{c['input_per_correct']:,.0f}" if c["input_per_correct"] else "- (none correct)"
        replayed = c["calls"]["pacds"]["replayed"] + c["calls"]["client"]["replayed"]
        if replayed:
            lines += ["", f"Replayed model calls: PACDS {c['calls']['pacds']['replayed']} of {sum(c['calls']['pacds'].values())}, "
                          f"client {c['calls']['client']['replayed']} of {sum(c['calls']['client'].values())}; "
                          f"live input tokens {c['live_input']:,} (the table counts replayed calls as recorded)."]
        lines += ["", f"Per attempt {c['input_per_attempt'] or 0:,.0f}, per correct decision {per_correct}."]
        if c["pacds_requests"]:
            lines[-1] += (f" PACDS requests {c['pacds_requests']}: cached {_pct(c['pacds_cached_share'])} of input; "
                          f"resent history {_pct(c['pacds_resent_share'])} (tool results {_pct(c['pacds_resent_tool_results_share'])}).")
        if c["pacds_input_by_source"]:
            total = sum(c["pacds_input_by_source"].values()) or 1
            lines += ["", "PACDS input by source (every call, resends included):", ""]
            lines += _table(["source", "tokens", "share"], [[k, f"{v:,}", _pct(v / total)] for k, v in c["pacds_input_by_source"].items()])
        lines += ["", "### Reliability", ""]
        for key, title in (("attempt_errors", "Failed attempts"), ("pacds_status", "PACDS status"), ("trace_errors", "Errors in traces"),
                           ("investigation_ended", "Investigations ended"), ("finish_reasons", "Finish reasons"),
                           ("failed_model_calls", "Failed model calls (retried or fatal), by status")):
            if r[key]:
                lines.append(f"- {title}: " + ", ".join(f"{k} {v}" for k, v in r[key].items()))
        if e.get("failure_modes"):
            modes = sorted({mode for counts in e["failure_modes"].values() for mode in counts})
            lines += ["", "### Failure modes (misses; pacds eval classify)", ""]
            lines += _table(["truth", *modes], [[truth, *(counts.get(mode, 0) for mode in modes)] for truth, counts in e["failure_modes"].items()])
        if b:
            lines += ["", "### Behavior", ""]
            if "checks" in b:
                lines.append(f"PACDS calls per ticket {b['pacds_calls_per_ticket']:.1f}, questions per ticket {b['questions_per_ticket']:.1f}. "
                             "Question checks (keyword heuristics, pacds_eval/analysis/checks.py):")
                lines += [""] + _table(["check", "tickets asking", "accuracy when asked", "when not", "asked by class"],
                                       [[k, f"{v['tickets']} ({_pct(v['share'])})", _pct(v["accuracy_when_asked"]), _pct(v["accuracy_when_not"]),
                                         " ".join(f"{c}:{n}" for c, n in v["by_class"].items())] for k, v in b["checks"].items()])
            if "pacds" in b:
                p = b["pacds"]
                lines += ["", f"PACDS investigations {p['requests']}: turns median {p['turns_median']}, mean {p['turns_mean'] or 0:.1f}; "
                              f"files read median {p['files_read_median']}; used git history {_pct(p['history_share'])}.",
                          "Tool calls: " + ", ".join(f"{k} {v}" for k, v in p["tools"].items())]
        lines.append("")
    return "\n".join(lines)


def write_report(run: Run, out: Path | None = None) -> Path:
    out = out or run.path / "report"
    out.mkdir(parents=True, exist_ok=True)
    report = build(run, out)
    (out / "report.md").write_text(render(report))
    (out / "costs.json").write_text(json.dumps({e["name"]: e["costs"] for e in report["evaluations"]}, indent=2) + "\n")
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    with (out / "cases.jsonl").open("w") as stream:
        for evaluation in run.evaluations:
            for record in case_records(run, evaluation):
                stream.write(json.dumps(record, default=str) + "\n")
    return out
