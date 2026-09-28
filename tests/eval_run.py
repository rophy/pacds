"""Evaluation run records for scripts/eval.sh: what ran, and which requests failed.

Usage: python -m tests.eval_run record RUN_DIR -- ARGS...   write RUN_DIR/run.json (commit, model, arguments)
       python -m tests.eval_run errors RUN_DIR              list failed requests in RUN_DIR/errors.json
Each failure carries its PACDS request id; RUN_DIR/compose.log has PACDS's log lines for it (request=<id>).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SKIP = {"run.json", "errors.json"}


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True).stdout.strip()


def record(run_dir: Path, args: list[str]) -> dict[str, Any]:
    info = {
        "started": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": _git("rev-parse", "HEAD"),
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(_git("status", "--porcelain", "--untracked-files=no")),
        "llm": {key: os.environ.get(f"LLM_{key.upper()}") for key in ("model", "base_url", "api")},
        "args": args,
    }
    (run_dir / "run.json").write_text(json.dumps(info, indent=2) + "\n")
    return info


def failures(result_file: Path) -> list[dict[str, Any]]:
    """Failed requests in one results file: the replay harness's or the support agent runner's --out."""
    data = json.loads(result_file.read_text())
    found = []
    for row in data.get("results", []):
        if "pacds_requests" in row:  # support agent: failed PACDS calls, and tickets the agent could not finish
            for call in row["pacds_requests"]:
                error = (call.get("reply") or {}).get("error")
                if error:
                    found.append({"file": result_file.name, "case_id": row["case_id"], "request_id": call.get("request_id"),
                                  "status": error.get("status"), "code": error.get("code"), "message": error.get("message")})
            if row.get("error"):
                found.append({"file": result_file.name, "case_id": row["case_id"], "request_id": None,
                              "status": None, "code": "agent_failed", "message": row["error"]})
        elif row.get("error"):  # replay harness
            found.append({"file": result_file.name, "case_id": row["case_id"], "request_id": row.get("request_id"),
                          "status": None, "code": None, "message": row["error"]})
    return found


def errors(run_dir: Path) -> list[dict[str, Any]]:
    found = [failure for path in sorted(run_dir.glob("*.json")) if path.name not in SKIP for failure in failures(path)]
    (run_dir / "errors.json").write_text(json.dumps(found, indent=2) + "\n")
    if found:
        print(f"=== {len(found)} failed requests: {run_dir / 'errors.json'}")
        print(f"    reasons: grep 'request=<request_id>' {run_dir / 'compose.log'}")
    else:
        print("=== no failed requests")
    return found


def main() -> None:
    command, run_dir = sys.argv[1], Path(sys.argv[2])
    if command == "record":
        record(run_dir, sys.argv[4:] if sys.argv[3:4] == ["--"] else sys.argv[3:])
    elif command == "errors":
        errors(run_dir)
    else:
        sys.exit(f"unknown command {command!r}")


if __name__ == "__main__":
    main()
