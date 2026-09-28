import json

from tests.replay.catalog import CATALOG, MODEL_CUTOFFS, load_all, render, source_of
from tests.replay.harness import CANDIDATES_DIR, load_cases

MAINTAINERS = ("Chris Cranford", "Jiri Pechanec", "Gunnar Morling")


def test_catalog_is_up_to_date():
    assert CATALOG.read_text() == render(load_all()), "run: python -m tests.replay.catalog"


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
    rows = [{"id": "new", "dir": "candidates/new", "set": "candidate", "truth": "D", "tier": "unreviewed", "source": "s",
             "created": "2099-01-01", "url": "u", "title": "t"},
            {"id": "old", "dir": "cases/old", "set": "hard", "truth": "B", "tier": "certain", "source": "s",
             "created": "2000-01-01", "url": "u", "title": "t"}]
    text = render(rows)
    assert f"| {model} | {cutoff} | 0 of 1 | 1 of 1 |" in text
    assert "| 2099-01-01 | [new](candidates/new/case.json) | candidate | D | unreviewed | [s](u) | ✓ | t |" in text
    assert "| 2000-01-01 | [old](cases/old/case.json) | hard | B | certain | [s](u) |  | t |" in text
