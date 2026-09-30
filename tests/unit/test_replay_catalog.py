import json
from pathlib import Path

from pacds_eval.catalog import MODEL_CUTOFFS, load_all, render, source_of
from pacds_eval.harness import load_cases

CASE_SETS = Path(__file__).parents[2] / "cases"
CANDIDATES_DIR = CASE_SETS / "debezium"

MAINTAINERS = ("Chris Cranford", "Jiri Pechanec", "Gunnar Morling")


def test_catalogs_are_up_to_date():
    for case_set in sorted(CASE_SETS.iterdir()):
        assert (case_set / "CATALOG.md").read_text() == render(load_all(case_set)), f"run: python -m pacds_eval.catalog --cases-dir cases/{case_set.name}"


def test_every_candidate_loads_with_its_logs():
    candidates = load_cases(CANDIDATES_DIR)
    assert candidates and {case.set for case in candidates} == {"candidate"}


def test_candidate_reports_hold_only_the_reporters_words():
    for path in CANDIDATES_DIR.glob("*/case.json"):
        case = json.loads(path.read_text())
        assert not any(name in case["report"] for name in MAINTAINERS), case["id"]
        assert case["source"] and case["created_at"] and case["ref"].startswith("v"), case["id"]


def test_source_comes_from_the_case_or_its_issue_url():
    assert source_of({"source": "google-group:debezium"}) == "google-group:debezium"
    assert source_of({"issue_url": "https://github.com/go-gitea/gitea/issues/1"}) == "github:go-gitea/gitea"


def test_render_marks_cases_created_after_a_model_cutoff():
    model, cutoff = next(iter(MODEL_CUTOFFS.items()))
    rows = [{"id": "new", "dir": "new", "set": "candidate", "truth": "D", "tier": "unreviewed", "status": "labeled", "source": "s",
             "created": "2099-01-01", "url": "u", "title": "t", "baseline": [0.2, 0.4]},
            {"id": "old", "dir": "old", "set": "hard", "truth": "B", "tier": "certain", "status": "labeled", "source": "s",
             "created": "2000-01-01", "url": "u", "title": "t"}]
    text = render(rows)
    assert f"| {model} | {cutoff} | 0 of 1 | 1 of 1 |" in text
    after_all = " | ".join("✓" for _ in MODEL_CUTOFFS)
    after_none = " | ".join("" for _ in MODEL_CUTOFFS)
    assert f"| 2099-01-01 | [new](new/case.json) | labeled | candidate | D | unreviewed | 0.30 | [s](u) | {after_all} | t |" in text
    assert f"| 2000-01-01 | [old](old/case.json) | labeled | hard | B | certain |  | [s](u) | {after_none} | t |" in text
