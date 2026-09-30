import json
from pathlib import Path

import pytest

from pacds_eval import casebook
from pacds_eval.casebook import (add_review, adjudicate, decide, import_tickets, label, load, packet, review_with_llm, screen,
                                   status)
from pacds_eval.harness import load_cases

TICKET = {"id": "crm-101", "repo": "https://git.corp.example/crm/app.git", "ref": "v4.2.0",
          "report": "Export to CSV drops the last row\n\nSince 4.2 the last row is missing.",
          "created_at": "2026-08-01", "source": "jira:CRM", "url": "https://jira.corp.example/CRM-101",
          "logs": ["logs/crm-101.log"], "resolution": "Off-by-one in ExportService.writeRows, fixed in 4.2.1 (commit abc)."}


@pytest.fixture
def export(tmp_path) -> Path:
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "crm-101.log").write_text("ERROR export truncated\n")
    path = tmp_path / "tickets.jsonl"
    path.write_text(json.dumps(TICKET) + "\n" + json.dumps({"id": "bad id", "repo": "git@x", "report": ""}) + "\n")
    return path


@pytest.fixture
def cases_dir(tmp_path) -> Path:
    return tmp_path / "cases"


def test_import_creates_drafts_copies_logs_and_reports_bad_rows(export, cases_dir):
    outcome = import_tickets(export, cases_dir)
    assert outcome["created"] == ["crm-101"] and len(outcome["errors"]) == 1
    assert "missing ref" in outcome["errors"][0] and "id may contain" in outcome["errors"][0] and "https" in outcome["errors"][0]
    case = load(cases_dir, "crm-101")
    assert (case["status"], case["set"], case["truth"], case["logs"]) == ("draft", "candidate", None, ["crm-101.log"])
    assert (cases_dir / "crm-101" / "crm-101.log").read_text() == "ERROR export truncated\n"
    assert load_cases(cases_dir) == []  # drafts are not evaluated


def test_reimport_skips_unless_updating_and_keeps_reviews(export, cases_dir):
    import_tickets(export, cases_dir)
    add_review(cases_dir, "crm-101", by="alice", cls="D", confidence="certain", fix="code fix")
    assert import_tickets(export, cases_dir)["skipped"] == ["crm-101"]
    assert import_tickets(export, cases_dir, update=True)["updated"] == ["crm-101"]
    assert len(load(cases_dir, "crm-101")["reviews"]) == 1


def test_csv_exports_split_logs(tmp_path, cases_dir):
    (tmp_path / "a.log").write_text("x")
    (tmp_path / "b.log").write_text("y")
    path = tmp_path / "tickets.csv"
    path.write_text("id,repo,ref,report,logs,resolution\nt1,https://git.corp.example/a.git,main,Broken,a.log; b.log,Config fix\n")
    assert import_tickets(path, cases_dir)["created"] == ["t1"]
    assert load(cases_dir, "t1")["logs"] == ["a.log", "b.log"]


def test_packet_is_blind(export, cases_dir):
    import_tickets(export, cases_dir)
    add_review(cases_dir, "crm-101", by="alice", cls="D", confidence="certain", fix="SECRET-FIX-TEXT")
    text = packet(cases_dir, "crm-101")
    assert "where must the fix be made" in text and "Off-by-one in ExportService" in text and "ERROR export truncated" in text
    assert "SECRET-FIX-TEXT" not in text and "alice" not in text


def test_reviews_are_validated(export, cases_dir):
    import_tickets(export, cases_dir)
    with pytest.raises(ValueError, match="class"):
        add_review(cases_dir, "crm-101", by="a", cls="E", confidence="certain", fix="f")
    add_review(cases_dir, "crm-101", by="a", cls="D", confidence="certain", fix="f")
    with pytest.raises(ValueError, match="already has a review by a"):
        add_review(cases_dir, "crm-101", by="a", cls="D", confidence="certain", fix="f")


@pytest.mark.parametrize("reviews, expected", [
    ([], ("draft", None, None)),
    ([("D", "certain")], ("in_review", None, None)),
    ([("D", "certain"), ("D", "certain")], ("labeled", "D", "certain")),
    ([("B", "certain"), ("B", "probable")], ("labeled", "B", "probable")),
    ([("B", "certain"), ("C", "certain")], ("disputed", None, None)),
    ([("drop", "certain"), ("drop", "probable")], ("rejected", None, None)),
    ([("drop", "certain"), ("D", "certain")], ("disputed", None, None)),
])
def test_decide(reviews, expected):
    assert decide([{"class": c, "confidence": k} for c, k in reviews]) == expected


def test_label_adjudicate_and_status(export, cases_dir):
    import_tickets(export, cases_dir)
    add_review(cases_dir, "crm-101", by="alice", cls="D", confidence="certain", fix="f")
    add_review(cases_dir, "crm-101", by="bob", cls="B", confidence="probable", fix="f")
    assert label(cases_dir) == {"disputed": 1}
    assert "disputed (adjudicate): crm-101" in status(cases_dir)
    case = adjudicate(cases_dir, "crm-101", cls="D", by="carol", note="the fix shipped in code")
    assert (case["status"], case["truth"], case["tier"]) == ("labeled", "D", "probable")
    assert label(cases_dir) == {} and [c.id for c in load_cases(cases_dir)] == ["crm-101"]
    with pytest.raises(ValueError, match="already labeled"):
        add_review(cases_dir, "crm-101", by="dave", cls="D", confidence="certain", fix="f")


def test_screen_marks_hard_and_clear_from_a_baseline_run(export, cases_dir, tmp_path):
    import_tickets(export, cases_dir)
    for by in ("a", "b"):
        add_review(cases_dir, "crm-101", by=by, cls="D", confidence="certain", fix="f")
    label(cases_dir)
    run = tmp_path / "run"
    run.mkdir()
    (run / "run.json").write_text(json.dumps({"llm": {"model": "served-model"}}))
    rows = [{"case_id": "crm-101", "truth": "D", "predicted": "C", "correct": False, "p_truth": p, "repeat": n} for n, p in ((1, 0.2), (2, 0.5))]
    (run / "replay-1.json").write_text(json.dumps({"baseline": True, "repeat": 2, "results": rows}))
    assert screen(cases_dir, run) == {"crm-101": "hard"}
    case = load(cases_dir, "crm-101")
    assert case["baseline_p_truth"] == [0.2, 0.5] and case["baseline_model"] == "served-model"


def test_llm_reviews_are_independent_and_recorded(export, cases_dir, monkeypatch):
    monkeypatch.setenv("LLM_API", "")  # chat completions, as vLLM serves
    import_tickets(export, cases_dir)
    calls = []

    class Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            content = json.dumps({"class": "D", "confidence": "certain", "fix": "code", "evidence": "Off-by-one", "boundary": None})
            return type("R", (), {"choices": [type("C", (), {"message": type("M", (), {"content": content})()})()]})()

    client = type("Client", (), {"chat": type("Chat", (), {"completions": Completions()})()})()
    done = review_with_llm(cases_dir, ["crm-101"], reviewers=2, client=client, model="m")
    assert len(done) == 2 and len(calls) == 2
    assert calls[0]["messages"][0]["content"] == casebook.REVIEW_PROMPT.read_text()
    assert [r["by"] for r in load(cases_dir, "crm-101")["reviews"]] == ["llm-1:m", "llm-2:m"]
    assert review_with_llm(cases_dir, ["crm-101"], reviewers=2, client=client, model="m") == []  # already reviewed
    assert label(cases_dir) == {"labeled": 1}


def test_external_catalog_shows_status(export, cases_dir):
    from pacds_eval.catalog import load_all, render

    import_tickets(export, cases_dir)
    text = render(load_all(cases_dir), cutoffs={"served-model": "2026-06-30"})
    assert "| draft | candidate | - | - |" in text and "served-model" in text and "gpt-6-luna" not in text


def test_llm_reviews_on_claude_code_need_no_client(export, cases_dir, monkeypatch):
    from pacds.engine import claude_code

    monkeypatch.setenv("LLM_API", "claude_code")
    monkeypatch.setenv("LLM_MODEL", "haiku")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    import_tickets(export, cases_dir)
    seen = []

    async def fake_ask_json(**kwargs):
        seen.append(kwargs)
        return {"class": "D", "confidence": "certain", "fix": "code", "evidence": "Off-by-one", "boundary": None}, {"input": 1, "output": 1}

    monkeypatch.setattr(claude_code, "ask_json", fake_ask_json)
    monkeypatch.setenv("LLM_EFFORT", "medium")
    monkeypatch.delenv("LLM_TIMEOUT_SECONDS", raising=False)
    done = review_with_llm(cases_dir, ["crm-101"], reviewers=2)
    assert len(done) == 2 and seen[0]["schema"] is casebook.REVIEW_SCHEMA and seen[0]["model"] == "haiku"
    assert seen[0]["effort"] == "medium" and seen[0]["timeout"] == 300  # the API path's llm_timeout(300)
    assert [r["by"] for r in load(cases_dir, "crm-101")["reviews"]] == ["llm-1:haiku", "llm-2:haiku"]
