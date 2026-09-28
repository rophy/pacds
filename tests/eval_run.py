"""Evaluation run records for scripts/eval.sh: what ran, and which requests failed.

Usage: python -m tests.eval_run record RUN_DIR -- ARGS...   write RUN_DIR/run.json (commit, model, arguments)
       python -m tests.eval_run manifest RUN_DIR [CONFIG]   add the prompt hashes and PACDS's resolved config (JSON file)
       python -m tests.eval_run errors RUN_DIR              list failed requests in RUN_DIR/errors.json
       python -m tests.eval_run finish RUN_DIR              add the evaluations, their cases and trace counts to run.json
       python -m tests.eval_run archive RUN_DIR             upload RUN_DIR as <name>.tar.gz to the run archive
       python -m tests.eval_run fetch NAME [DEST]           download and unpack an archived run (default into eval-runs/)
       python -m tests.eval_run list                        list archived runs
Each failure carries its PACDS request id; RUN_DIR/compose.log has PACDS's log lines for it (request=<id>), and
RUN_DIR/traces/pacds/<id>.json the whole investigation.
The run archive is S3: PACDS_EVAL_ARCHIVE_S3_URI (s3://bucket/prefix/), PACDS_EVAL_ARCHIVE_REGION,
PACDS_EVAL_ARCHIVE_ACCESS_KEY_ID, PACDS_EVAL_ARCHIVE_SECRET_ACCESS_KEY. Runs hold source code in their traces:
archive runs on a private repository only where that code may be stored.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pacds.engine.trace import sha256

SKIP = {"run.json", "errors.json", "pacds-config.json"}
ARCHIVE_ENV = "PACDS_EVAL_ARCHIVE_S3_URI"
# Presigned signatures are credentials; results and traces must not carry them.
SIGNATURE = re.compile(r"(X-Amz-Signature=)[^&\s\"\\]+")


def redact(text: str) -> str:
    return SIGNATURE.sub(r"\1REDACTED", text)


def repeats(cases: list[Any]) -> list[tuple[Any, int]]:
    """Pair each case of `cases * N` with its repeat number (1-based), in order."""
    seen: dict[str, int] = {}
    paired = []
    for case in cases:
        seen[case.id] = seen.get(case.id, 0) + 1
        paired.append((case, seen[case.id]))
    return paired


def write_trace(trace_dir: Path | None, case_id: str, repeat: int, trace: Any) -> None:
    """Write a client-side trace (support agent, baseline) as TRACE_DIR/<case>-<repeat>.json."""
    if trace_dir is None:
        return
    trace_dir.mkdir(parents=True, exist_ok=True)
    (trace_dir / f"{case_id}-{repeat}.json").write_text(redact(json.dumps(trace.to_dict(), default=str)))


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


def _update(run_dir: Path, **fields: Any) -> dict[str, Any]:
    path = run_dir / "run.json"
    info = json.loads(path.read_text()) if path.exists() else {}
    info.update(fields)
    path.write_text(json.dumps(info, indent=2) + "\n")
    return info


def prompt_hashes() -> dict[str, str]:
    """SHA-256 of every prompt, tool definition and skill the evaluated components send to a model."""
    from pacds.engine import agent_provider
    from pacds.engine.tools import TOOL_DEFINITIONS
    from tests.replay import baseline, harness
    from tests.support_agent import agent

    hashes = {
        "pacds.system": sha256(agent_provider.AGENT_SYSTEM_PROMPT),
        "pacds.final": sha256(agent_provider.FINAL_INSTRUCTION),
        "pacds.schema_instruction": sha256(agent_provider.SCHEMA_INSTRUCTION),
        "pacds.premature_ready": sha256(agent_provider.PREMATURE_READY),
        "pacds.tools": sha256([*TOOL_DEFINITIONS, agent_provider.READY_TOOL]),
        "baseline.system": sha256(baseline.BASELINE_SYSTEM_PROMPT),
        "replay.question": sha256({"question": harness.QUESTION, "criteria": harness.CRITERIA}),
        "agent.tools": sha256([agent.CALL_PACDS_TOOL, agent.SUBMIT_DECISION_TOOL]),
    }
    for skill in sorted(agent.SKILLS_DIR.glob("*/SKILL.md")):
        hashes[f"skill.{skill.parent.name}"] = sha256(skill.read_text())
    return hashes


def manifest(run_dir: Path, config_file: Path | None = None) -> dict[str, Any]:
    fields: dict[str, Any] = {"prompts": prompt_hashes()}
    if config_file is not None and config_file.is_file() and config_file.stat().st_size:
        fields["pacds_config"] = json.loads(config_file.read_text())
    return _update(run_dir, **fields)


def finish(run_dir: Path) -> dict[str, Any]:
    """Record what each results file evaluated: its cases with their labels, repeats, and the traces kept."""
    evaluations, cases = [], {}
    for path in sorted(run_dir.glob("*.json")):
        if path.name in SKIP:
            continue
        data = json.loads(path.read_text())
        kind = "support" if "variant" in data else "replay"
        traces = run_dir / "traces" / path.stem
        evaluations.append({
            "file": path.name, "kind": kind, "variant": data.get("variant") or ("baseline" if data.get("baseline") else "pacds"),
            "repeat": data.get("repeat"), "cases": [case["id"] for case in data.get("cases", [])],
            "results": len(data.get("results", [])), "traces": len(list(traces.glob("*.json"))) if traces.is_dir() else 0,
        })
        cases.update({case["id"]: case for case in data.get("cases", [])})
    pacds_traces = run_dir / "traces" / "pacds"
    return _update(run_dir, finished=datetime.now(UTC).isoformat(timespec="seconds"), evaluations=evaluations,
                   cases=sorted(cases.values(), key=lambda case: case["id"]),
                   pacds_traces=len(list(pacds_traces.glob("*.json"))) if pacds_traces.is_dir() else 0)


def _archive() -> tuple[Any, str, str]:
    import boto3

    uri = os.environ.get(ARCHIVE_ENV)
    if not uri:
        sys.exit(f"{ARCHIVE_ENV} is not set")
    parts = urlsplit(uri)
    # Explicit keys: the generic AWS_* variables may belong to something else (e.g. an egress proxy).
    client = boto3.client("s3", region_name=os.environ.get("PACDS_EVAL_ARCHIVE_REGION"),
                          aws_access_key_id=os.environ.get("PACDS_EVAL_ARCHIVE_ACCESS_KEY_ID"),
                          aws_secret_access_key=os.environ.get("PACDS_EVAL_ARCHIVE_SECRET_ACCESS_KEY"))
    prefix = parts.path.strip("/")
    return client, parts.netloc, f"{prefix}/" if prefix else ""


def archive(run_dir: Path) -> str:
    client, bucket, prefix = _archive()
    with tempfile.TemporaryDirectory() as tmp:
        tarball = Path(tmp) / f"{run_dir.name}.tar.gz"
        with tarfile.open(tarball, "w:gz") as tar:
            tar.add(run_dir, arcname=run_dir.name)
        client.upload_file(str(tarball), bucket, f"{prefix}{run_dir.name}.tar.gz")
    # run.json alone as well, so runs can be listed and compared without downloading them.
    if (run_dir / "run.json").is_file():
        client.upload_file(str(run_dir / "run.json"), bucket, f"{prefix}{run_dir.name}.run.json")
    location = f"s3://{bucket}/{prefix}{run_dir.name}.tar.gz"
    print(f"=== archived: {location}")
    return location


def fetch(name: str, dest: Path) -> Path:
    client, bucket, prefix = _archive()
    name = name.removesuffix(".tar.gz")
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tarball = Path(tmp) / f"{name}.tar.gz"
        client.download_file(bucket, f"{prefix}{name}.tar.gz", str(tarball))
        with tarfile.open(tarball) as tar:
            tar.extractall(dest, filter="data")
    print(f"=== fetched: {dest / name}")
    return dest / name


def list_archived() -> list[str]:
    client, bucket, prefix = _archive()
    names = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            key = item["Key"].removeprefix(prefix)
            if key.endswith(".tar.gz") and "/" not in key:
                names.append(key.removesuffix(".tar.gz"))
                print(f"{names[-1]}  {item['Size'] / 1e6:.1f} MB")
    return names


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
    command, rest = sys.argv[1], sys.argv[2:]
    if command == "record":
        record(Path(rest[0]), rest[2:] if rest[1:2] == ["--"] else rest[1:])
    elif command == "manifest":
        manifest(Path(rest[0]), Path(rest[1]) if len(rest) > 1 else None)
    elif command == "errors":
        errors(Path(rest[0]))
    elif command == "finish":
        finish(Path(rest[0]))
    elif command == "archive":
        archive(Path(rest[0]))
    elif command == "fetch":
        fetch(rest[0], Path(rest[1]) if len(rest) > 1 else Path("eval-runs"))
    elif command == "list":
        list_archived()
    else:
        sys.exit(f"unknown command {command!r}")


if __name__ == "__main__":
    main()
