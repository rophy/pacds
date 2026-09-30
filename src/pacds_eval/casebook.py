"""Case sets for evaluations: import tickets, review them blind, label, screen, and report status.

A case set is a directory anywhere (corporate cases never belong in this repository):
  <case set>/<case id>/case.json + the case's log files, regression-sample.json, CATALOG.md
A case moves through: draft (imported) -> in_review (reviews arriving) -> labeled (reviews agree: truth and tier set)
| disputed (reviews disagree; a person adjudicates) | rejected (reviewers say it cannot be a test case).
Only labeled cases are evaluated. Screening with the no-code baseline then marks each labeled case hard or clear.

Usage: python -m pacds_eval.casebook import EXPORT --cases-dir DIR [--update]
       python -m pacds_eval.casebook packet --cases-dir DIR CASE              blind review packet (Markdown)
       python -m pacds_eval.casebook review --cases-dir DIR CASE --by NAME --class A|B|C|D|drop
                                              --confidence certain|probable --fix TEXT [--evidence TEXT] [--boundary TEXT]
       python -m pacds_eval.casebook review-llm --cases-dir DIR [CASE ...] [--reviewers 2]   blind reviews by LLM_*
       python -m pacds_eval.casebook label --cases-dir DIR
       python -m pacds_eval.casebook adjudicate --cases-dir DIR CASE --class A|B|C|D|drop --by NAME --note TEXT
       python -m pacds_eval.casebook screen --cases-dir DIR --from-run RUN   baseline p(truth) -> hard / clear
       python -m pacds_eval.casebook status --cases-dir DIR
EXPORT is JSON Lines, a JSON array, or CSV with the fields in TICKET_FIELDS (docs/evaluation-runbook.md).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CLASSES = ("A", "B", "C", "D")
DROP = "drop"
CONFIDENCE = ("certain", "probable")
STATUSES = ("draft", "in_review", "labeled", "disputed", "rejected")
# Fields of a ticket export. Required: id, repo, ref, report. resolution is shown to reviewers, never to PACDS or the
# agent: how the ticket was actually resolved (the maintainer's conclusion, the fix, the change that closed it).
TICKET_FIELDS = ("id", "repo", "ref", "report", "created_at", "source", "url", "logs", "resolution")
REQUIRED = ("id", "repo", "ref", "report")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_LOG_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
REVIEWS_NEEDED = 2
HARD_THRESHOLD = 0.6  # a case is hard when the no-code baseline's mean p(truth) is at most this
PLAYBOOK = Path(__file__).parent / "skills" / "tech-support" / "SKILL.md"
REVIEW_PROMPT = Path(__file__).parent / "prompts" / "review.md"


def default_cases_dir() -> Path | None:
    value = os.environ.get("PACDS_CASES_DIR")
    return Path(value) if value else None


def case_path(cases_dir: Path, case_id: str) -> Path:
    return Path(cases_dir) / case_id / "case.json"


def load(cases_dir: Path, case_id: str) -> dict[str, Any]:
    path = case_path(cases_dir, case_id)
    if not path.is_file():
        raise ValueError(f"no case {case_id} in {cases_dir}")
    return json.loads(path.read_text())


def save(cases_dir: Path, case: dict[str, Any]) -> None:
    case_path(cases_dir, case["id"]).write_text(json.dumps(case, indent=2, ensure_ascii=False) + "\n")


def all_cases(cases_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted(Path(cases_dir).glob("*/case.json"))]


def status_of(case: dict[str, Any]) -> str:
    """Cases written before statuses existed are labeled when they carry a truth."""
    return case.get("status") or ("labeled" if case.get("truth") else "draft")


# --- import ------------------------------------------------------------------------------------------------------

def read_export(path: Path) -> list[dict[str, Any]]:
    text = path.read_text()
    if path.suffix.lower() == ".csv":
        rows = [dict(row) for row in csv.DictReader(text.splitlines())]
        for row in rows:  # logs: file paths separated by ';'
            row["logs"] = [part.strip() for part in (row.get("logs") or "").split(";") if part.strip()]
        return rows
    stripped = text.lstrip()
    if stripped.startswith("["):
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def validate_ticket(ticket: dict[str, Any]) -> list[str]:
    problems = [f"missing {field}" for field in REQUIRED if not str(ticket.get(field) or "").strip()]
    if ticket.get("id") and not _ID.fullmatch(str(ticket["id"])):
        problems.append("id may contain only letters, digits, '.', '_' and '-'")
    if ticket.get("repo") and not str(ticket["repo"]).startswith("https://"):
        problems.append("repo must be an https git URL")
    unknown = sorted(set(ticket) - set(TICKET_FIELDS))
    if unknown:
        problems.append(f"unknown fields {', '.join(unknown)}")
    return problems


def import_tickets(export: Path, cases_dir: Path, *, update: bool = False) -> dict[str, list[str]]:
    """Create draft cases from a ticket export; logs are copied next to case.json. Returns created/updated/skipped/errors."""
    outcome: dict[str, list[str]] = {"created": [], "updated": [], "skipped": [], "errors": []}
    for ticket in read_export(export):
        problems = validate_ticket(ticket)
        logs = [export.parent / log for log in ticket.get("logs") or []]
        problems += [f"log {log} not found" for log in logs if not log.is_file()]
        problems += [f"log file name {log.name} not allowed" for log in logs if not _LOG_NAME.fullmatch(log.name)]
        if problems:
            outcome["errors"].append(f"{ticket.get('id', '?')}: {'; '.join(problems)}")
            continue
        path = case_path(cases_dir, ticket["id"])
        existing = json.loads(path.read_text()) if path.is_file() else None
        if existing and not update:
            outcome["skipped"].append(ticket["id"])
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        for log in logs:
            shutil.copyfile(log, path.parent / log.name)
        case = existing or {"status": "draft", "set": "candidate", "truth": None, "reviews": []}
        case.update({
            "id": ticket["id"], "repo": ticket["repo"], "ref": ticket["ref"], "report": ticket["report"],
            "logs": [log.name for log in logs], "created_at": str(ticket.get("created_at") or ""),
            "source": ticket.get("source") or "import", "issue_url": ticket.get("url") or "",
            "resolution": ticket.get("resolution") or "",
            "imported": {"from": export.name, "at": datetime.now(UTC).isoformat(timespec="seconds")},
        })
        save(cases_dir, case)
        outcome["updated" if existing else "created"].append(ticket["id"])
    return outcome


# --- blind review --------------------------------------------------------------------------------------------------

def playbook_rules() -> str:
    """The playbook's classes, fix-location rule and boundary cases: reviewers label by the rule the agent decides by."""
    text = PLAYBOOK.read_text()
    start, end = text.index("## The four classes"), text.index("## Procedure")
    return text[start:end].strip()


def _clip(text: str, limit: int = 6000) -> str:
    return text if len(text) <= limit else f"{text[: limit // 2]}\n…[{len(text) - limit} characters cut]…\n{text[-limit // 2:]}"


def packet(cases_dir: Path, case_id: str) -> str:
    """What a blind reviewer sees: the rules, the ticket, its logs and how it was resolved; never other reviews or a truth."""
    case = load(cases_dir, case_id)
    parts = [f"# Blind review: {case_id}", "", "Classify this ticket by the rules below, using how it was actually resolved.",
             "Answer with a class (A, B, C, D, or drop when it cannot serve as a test case: no clear resolution, several",
             "unrelated problems, or not about this application), your confidence (certain or probable), the fix in one",
             "sentence, the evidence (quote the resolution), and the boundary question if two classes were plausible.", "",
             playbook_rules(), "", "## The ticket", "", f"Repository: {case['repo']} at {case['ref']}", "", case["report"], ""]
    for name in case.get("logs", []):
        log = case_path(cases_dir, case_id).parent / name
        parts += [f"## Attached log: {name}", "", "```", _clip(log.read_text(errors="replace")), "```", ""]
    evidence = case.get("resolution") or (json.dumps(case["evidence"], ensure_ascii=False) if case.get("evidence") else "(none recorded)")
    parts += ["## How it was resolved", "", evidence, ""]
    return "\n".join(parts)


def add_review(cases_dir: Path, case_id: str, *, by: str, cls: str, confidence: str, fix: str,
               evidence: str = "", boundary: str | None = None) -> dict[str, Any]:
    if cls not in (*CLASSES, DROP):
        raise ValueError(f"class must be one of {', '.join(CLASSES)} or {DROP}")
    if confidence not in CONFIDENCE:
        raise ValueError(f"confidence must be {' or '.join(CONFIDENCE)}")
    case = load(cases_dir, case_id)
    if status_of(case) in ("labeled", "rejected"):
        raise ValueError(f"{case_id} is already {status_of(case)}")
    if any(review.get("by") == by for review in case.get("reviews", [])):
        raise ValueError(f"{case_id} already has a review by {by}")
    case.setdefault("reviews", []).append({"by": by, "class": cls, "confidence": confidence, "fix": fix, "evidence": evidence,
                                           "boundary": boundary, "at": datetime.now(UTC).isoformat(timespec="seconds")})
    case["status"] = "in_review"
    save(cases_dir, case)
    return case


REVIEW_SCHEMA = {
    "type": "object",
    "properties": {"class": {"type": "string", "enum": [*CLASSES, DROP]}, "confidence": {"type": "string", "enum": list(CONFIDENCE)},
                   "fix": {"type": "string"}, "evidence": {"type": "string"}, "boundary": {"type": ["string", "null"]}},
    "required": ["class", "confidence", "fix", "evidence", "boundary"],
    "additionalProperties": False,
}


def review_with_llm(cases_dir: Path, case_ids: list[str], *, reviewers: int = REVIEWS_NEEDED, client: Any = None,
                    model: str | None = None) -> list[str]:
    """Add blind LLM reviews ("llm-1", "llm-2", ...) until each case has `reviewers` of them; independent calls."""
    import openai

    from pacds_eval.runs import json_request, llm_extra_body, llm_timeout, parse_json

    from pacds.engine import responses_api

    model = model or os.environ["LLM_MODEL"]
    api = os.environ.get("LLM_API") or "chat_completions"
    if client is None and api != "claude_code":
        header = os.environ.get("LLM_SESSION_HEADER")  # providers that route by session (e.g. x-opencode-session)
        client = openai.OpenAI(base_url=os.environ["LLM_BASE_URL"], api_key=os.environ.get("LLM_API_KEY") or "not-needed", max_retries=2,
                               timeout=llm_timeout(300), default_headers={header: "casebook-review"} if header else None)
    passthrough = {"extra_body": llm_extra_body()} if llm_extra_body() else {}
    done = []
    for case_id in case_ids:
        case = load(cases_dir, case_id)
        if status_of(case) in ("labeled", "rejected"):
            continue
        for n in range(1, reviewers + 1):
            by = f"llm-{n}:{model}"
            if any(review.get("by") == by for review in load(cases_dir, case_id).get("reviews", [])):
                continue
            messages = [{"role": "system", "content": REVIEW_PROMPT.read_text()}, {"role": "user", "content": packet(cases_dir, case_id)}]
            request = json_request(messages, "review", REVIEW_SCHEMA)
            if api == "claude_code":
                import asyncio

                from pacds.engine import claude_code

                answer, _ = asyncio.run(claude_code.ask_json(system=REVIEW_PROMPT.read_text(), prompt=packet(cases_dir, case_id),
                                                             schema=REVIEW_SCHEMA, model=model, effort=os.environ.get("LLM_EFFORT") or None,
                                                             timeout=llm_timeout(300)))
                add_review(cases_dir, case_id, by=by, cls=answer["class"], confidence=answer["confidence"], fix=answer["fix"],
                           evidence=answer.get("evidence") or "", boundary=answer.get("boundary"))
                done.append(f"{case_id} {by}: {answer['class']} ({answer['confidence']})")
                continue
            if api == "responses":
                response = responses_api.from_response(client.responses.create(
                    model=model, **responses_api.request_kwargs(**request), **passthrough))
            else:
                response = client.chat.completions.create(model=model, **request, **passthrough)
            answer = parse_json(response.choices[0].message.content)
            add_review(cases_dir, case_id, by=by, cls=answer["class"], confidence=answer["confidence"], fix=answer["fix"],
                       evidence=answer.get("evidence") or "", boundary=answer.get("boundary"))
            done.append(f"{case_id} {by}: {answer['class']} ({answer['confidence']})")
    return done


# --- labeling ------------------------------------------------------------------------------------------------------

def decide(reviews: list[dict[str, Any]], needed: int = REVIEWS_NEEDED) -> tuple[str, str | None, str | None]:
    """(status, truth, tier) from the reviews: agreement labels, all-drop rejects, anything else is disputed."""
    if len(reviews) < needed:
        return ("in_review" if reviews else "draft"), None, None
    classes = {review["class"] for review in reviews}
    if classes == {DROP}:
        return "rejected", None, None
    if len(classes) == 1:
        tier = "certain" if all(review["confidence"] == "certain" for review in reviews) else "probable"
        return "labeled", classes.pop(), tier
    return "disputed", None, None


def label(cases_dir: Path, needed: int = REVIEWS_NEEDED) -> Counter[str]:
    changes: Counter[str] = Counter()
    for case in all_cases(cases_dir):
        if status_of(case) in ("labeled", "rejected") or case.get("adjudication"):
            continue
        status, truth, tier = decide(case.get("reviews", []), needed)
        if status != status_of(case):
            case.update(status=status, truth=truth, **({"tier": tier} if tier else {}))
            save(cases_dir, case)
            changes[status] += 1
    return changes


def adjudicate(cases_dir: Path, case_id: str, *, cls: str, by: str, note: str) -> dict[str, Any]:
    """A person's final call on a disputed case. The tier is probable: the reviewers did not agree."""
    if cls not in (*CLASSES, DROP):
        raise ValueError(f"class must be one of {', '.join(CLASSES)} or {DROP}")
    case = load(cases_dir, case_id)
    if status_of(case) != "disputed":
        raise ValueError(f"{case_id} is {status_of(case)}, not disputed")
    case["adjudication"] = {"by": by, "class": cls, "note": note, "at": datetime.now(UTC).isoformat(timespec="seconds")}
    if cls == DROP:
        case.update(status="rejected", truth=None)
    else:
        case.update(status="labeled", truth=cls, tier="probable")
    save(cases_dir, case)
    return case


def screen(cases_dir: Path, run: Path) -> dict[str, str]:
    """Record the no-code baseline's p(truth) per case from a baseline replay run; mark hard (<= 0.6 mean) or clear."""
    from pacds_eval.analysis.load import load_runs, parse_runs

    loaded = load_runs(parse_runs(run))
    baselines = [e for e in loaded.evaluations if e.kind == "replay" and e.variant == "baseline"]
    if not baselines:
        raise ValueError(f"{run} has no baseline replay (run it with --baseline)")
    marked = {}
    for evaluation in baselines:
        for case_id, attempts in evaluation.by_case().items():
            path = case_path(cases_dir, case_id)
            if not path.is_file():
                continue
            case = json.loads(path.read_text())
            if status_of(case) != "labeled":
                continue
            p = [a.row.get("p_truth") for a in attempts if a.row.get("p_truth") is not None]
            if not p:
                continue
            case["baseline_p_truth"] = [round(x, 2) for x in p]
            case["baseline_model"] = loaded.info.get("llm", {}).get("model")
            case["set"] = "hard" if sum(p) / len(p) <= HARD_THRESHOLD else "clear"
            save(cases_dir, case)
            marked[case_id] = case["set"]
    return marked


def status(cases_dir: Path) -> str:
    cases = all_cases(cases_dir)
    by_status = Counter(status_of(case) for case in cases)
    labeled = [case for case in cases if status_of(case) == "labeled"]
    lines = [f"{len(cases)} cases in {cases_dir}", "  " + ", ".join(f"{s} {by_status.get(s, 0)}" for s in STATUSES)]
    if labeled:
        lines.append("  labeled by class: " + ", ".join(f"{c} {sum(case.get('truth') == c for case in labeled)}" for c in CLASSES))
        lines.append("  labeled by set: " + ", ".join(f"{s} {n}" for s, n in sorted(Counter(case.get('set', 'candidate') for case in labeled).items())))
        lines.append("  labeled by tier: " + ", ".join(f"{t} {n}" for t, n in sorted(Counter(case.get('tier') for case in labeled).items())))
    disputed = [case["id"] for case in cases if status_of(case) == "disputed"]
    if disputed:
        lines.append("  disputed (adjudicate): " + " ".join(disputed))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m pacds_eval.casebook", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    def with_dir(sub: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sub.add_argument("--cases-dir", type=Path, default=default_cases_dir(), required=default_cases_dir() is None,
                         help="the case set directory (default $PACDS_CASES_DIR)")
        return sub

    imp = with_dir(commands.add_parser("import", help="create draft cases from a ticket export"))
    imp.add_argument("export", type=Path)
    imp.add_argument("--update", action="store_true", help="refresh cases that already exist (keeps their reviews)")
    pkt = with_dir(commands.add_parser("packet", help="print a blind review packet"))
    pkt.add_argument("case")
    rev = with_dir(commands.add_parser("review", help="record a review"))
    rev.add_argument("case")
    rev.add_argument("--by", required=True)
    rev.add_argument("--class", dest="cls", required=True)
    rev.add_argument("--confidence", required=True, choices=CONFIDENCE)
    rev.add_argument("--fix", required=True)
    rev.add_argument("--evidence", default="")
    rev.add_argument("--boundary")
    llm = with_dir(commands.add_parser("review-llm", help="blind reviews by the LLM in LLM_*"))
    llm.add_argument("cases", nargs="*", help="default: every draft or in-review case")
    llm.add_argument("--reviewers", type=int, default=REVIEWS_NEEDED)
    with_dir(commands.add_parser("label", help="label cases whose reviews agree"))
    adj = with_dir(commands.add_parser("adjudicate", help="decide a disputed case"))
    adj.add_argument("case")
    adj.add_argument("--class", dest="cls", required=True)
    adj.add_argument("--by", required=True)
    adj.add_argument("--note", required=True)
    scr = with_dir(commands.add_parser("screen", help="mark labeled cases hard or clear from a baseline run"))
    scr.add_argument("--from-run", type=Path, required=True)
    with_dir(commands.add_parser("status", help="counts by status, class, set and tier"))
    args = parser.parse_args()

    try:
        if args.command == "import":
            outcome = import_tickets(args.export, args.cases_dir, update=args.update)
            for key, items in outcome.items():
                print(f"{key}: {len(items)}" + ("".join(f"\n  {item}" for item in items) if key == "errors" else ""))
            if outcome["errors"]:
                sys.exit(1)
        elif args.command == "packet":
            print(packet(args.cases_dir, args.case))
        elif args.command == "review":
            case = add_review(args.cases_dir, args.case, by=args.by, cls=args.cls, confidence=args.confidence, fix=args.fix,
                              evidence=args.evidence, boundary=args.boundary)
            print(f"{args.case}: {len(case['reviews'])} review(s)")
        elif args.command == "review-llm":
            ids = args.cases or [c["id"] for c in all_cases(args.cases_dir) if status_of(c) in ("draft", "in_review")]
            for line in review_with_llm(args.cases_dir, ids, reviewers=args.reviewers):
                print(line)
        elif args.command == "label":
            print(f"labeled {dict(label(args.cases_dir))}\n{status(args.cases_dir)}")
        elif args.command == "adjudicate":
            case = adjudicate(args.cases_dir, args.case, cls=args.cls, by=args.by, note=args.note)
            print(f"{args.case}: {case['status']} {case.get('truth') or ''}")
        elif args.command == "screen":
            marked = screen(args.cases_dir, args.from_run)
            print(f"screened {len(marked)}: " + ", ".join(f"{s} {n}" for s, n in sorted(Counter(marked.values()).items())))
        elif args.command == "status":
            print(status(args.cases_dir))
    except ValueError as error:
        sys.exit(f"error: {error}")


if __name__ == "__main__":
    main()
