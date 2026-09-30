"""`pacds eval run --target` against the Compose dev stack with the fake LLM (scripts/e2e.sh)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "cases" / "github"
BASE_URL = os.environ.get("PACDS_URL", "http://localhost:3002")


def clear_case() -> str:
    for path in sorted(CASES.glob("*/case.json")):
        data = json.loads(path.read_text())
        if data.get("set", "clear") == "clear" and data.get("truth") and data.get("status", "labeled") == "labeled":
            return data["id"]
    raise AssertionError("no clear case in cases/github")


def test_eval_run_replays_one_case_and_finishes_the_run(tmp_path):
    run_dir = tmp_path / "run"
    env = {k: v for k, v in os.environ.items() if k not in ("PACDS_EVAL_ARCHIVE_S3_URI", "PACDS_TRACE_SOURCE_DIR")}
    result = subprocess.run(
        [sys.executable, "-m", "pacds.cli", "eval", "run", "--target", BASE_URL, "--cases-dir", str(CASES),
         "--no-seed", "--run-dir", str(run_dir), "--replay", f"--set clear --case {clear_case()}"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=900)
    assert result.returncode == 0, result.stdout + result.stderr
    for name in ("run.json", "replay-1.json", "errors.json", "eval.log", "report/report.md"):
        assert (run_dir / name).is_file(), name
    assert f"=== report: {run_dir}/report/report.md" in result.stdout
