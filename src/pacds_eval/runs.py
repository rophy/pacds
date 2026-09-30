"""Evaluation run records for scripts/eval.sh: what ran, and which requests failed.

Usage: python -m pacds_eval.runs record RUN_DIR -- ARGS...   write RUN_DIR/run.json (commit, model, arguments)
       python -m pacds_eval.runs manifest RUN_DIR [CONFIG]   add the prompt hashes and PACDS's resolved config (JSON file)
       python -m pacds_eval.runs errors RUN_DIR              list failed requests in RUN_DIR/errors.json
       python -m pacds_eval.runs finish RUN_DIR              add the evaluations, their cases and trace counts to run.json
       python -m pacds_eval.runs sync RUN_DIR                upload RUN_DIR's new or changed files to <name>/ (during a run)
       python -m pacds_eval.runs collect-traces RUN_DIR SRC  copy the PACDS traces of this run's requests from SRC (a
                                                            deployed evaluation PACDS's trace directory) to RUN_DIR/traces/pacds
       python -m pacds_eval.runs archive RUN_DIR             upload RUN_DIR as <name>.tar.gz to the run archive
       python -m pacds_eval.runs fetch NAME [DEST]           download an archived run (default into eval-runs/): the
                                                            tarball, or the synced files of a run that never finished
       python -m pacds_eval.runs list                        list archived runs
Each failure carries its PACDS request id; RUN_DIR/compose.log has PACDS's log lines for it (request=<id>), and
RUN_DIR/traces/pacds/<id>.json the whole investigation.
The run archive is S3 or S3-compatible: PACDS_EVAL_ARCHIVE_S3_URI (s3://bucket/prefix/), PACDS_EVAL_ARCHIVE_REGION,
PACDS_EVAL_ARCHIVE_ACCESS_KEY_ID, PACDS_EVAL_ARCHIVE_SECRET_ACCESS_KEY, and PACDS_EVAL_ARCHIVE_ENDPOINT for
MinIO/Ceph (e.g. https://minio.corp.example; AWS_CA_BUNDLE for a corporate CA). Runs hold source code in their traces:
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
from pacds.version import package_version

SKIP = {"run.json", "errors.json", "pacds-config.json", "audit.json", ".synced.json"}
SYNC_STATE = ".synced.json"
ARCHIVE_ENV = "PACDS_EVAL_ARCHIVE_S3_URI"
# Presigned signatures are credentials; results and traces must not carry them.
SIGNATURE = re.compile(r"(X-Amz-Signature=)[^&\s\"\\]+")


def redact(text: str) -> str:
    return SIGNATURE.sub(r"\1REDACTED", text)


def llm_timeout(default: float = 120, env: Any = os.environ) -> float:
    """LLM_TIMEOUT_SECONDS: per model call on the client side, at least the caller's default; raise it for a slow server."""
    return max(default, float(env.get("LLM_TIMEOUT_SECONDS") or 0))


def llm_structured_outputs(env: Any = os.environ) -> bool:
    """LLM_STRUCTURED_OUTPUTS=false: schema in the prompt instead of json_schema, as PACDS's llm.structured_outputs."""
    return (env.get("LLM_STRUCTURED_OUTPUTS") or "true").lower() not in ("false", "0", "no")


def json_request(messages: list[dict[str, Any]], name: str, schema: dict[str, Any], env: Any = os.environ) -> dict[str, Any]:
    """Request arguments for a JSON answer: response_format json_schema, or with LLM_STRUCTURED_OUTPUTS=false the
    schema appended to the system message (for servers or models that reject json_schema). Parse with parse_json."""
    if llm_structured_outputs(env):
        return {"messages": messages, "response_format": {"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}}}
    instruction = f"\n\nAnswer with a single JSON object, no other text, matching this JSON schema:\n{json.dumps(schema)}"
    return {"messages": [{**messages[0], "content": messages[0]["content"] + instruction}, *messages[1:]]}


def parse_json(text: str | None) -> dict[str, Any]:
    """A JSON object from a model answer, tolerating a ```json fence around it."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return json.loads(text or "{}")


def llm_extra_body(env: Any = os.environ) -> dict[str, Any]:
    """LLM_EXTRA_BODY: a JSON object passed to the client-side model server as-is (support agent, baseline,
    classifier), e.g. {"chat_template_kwargs": {"enable_thinking": true}} for vLLM. PACDS has llm.extra_body."""
    raw = env.get("LLM_EXTRA_BODY") or ""
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("LLM_EXTRA_BODY must be a JSON object")
    return value


def repeats(cases: list[Any]) -> list[tuple[Any, int]]:
    """Pair each case of `cases * N` with its repeat number (1-based), in order."""
    seen: dict[str, int] = {}
    paired = []
    for case in cases:
        seen[case.id] = seen.get(case.id, 0) + 1
        paired.append((case, seen[case.id]))
    return paired


def client_recordings(runs: str | None) -> Any:
    """Recorded client calls (support agent, baseline) of RUN[,RUN...], for --replay-from; None without runs."""
    if not runs:
        return None
    from pacds.engine.replay import Recordings

    directories = [d for run in str(runs).split(",") if run for d in sorted((Path(run) / "traces").glob("*"))
                   if d.is_dir() and d.name != "pacds"]
    recordings = Recordings.from_dirs(directories)
    print(f"replaying from {runs}: {recordings.recorded} recorded client calls")
    return recordings


def write_trace(trace_dir: Path | None, case_id: str, repeat: int, trace: Any) -> None:
    """Write a client-side trace (support agent, baseline) as TRACE_DIR/<case>-<repeat>.json."""
    if trace_dir is None:
        return
    trace_dir.mkdir(parents=True, exist_ok=True)
    (trace_dir / f"{case_id}-{repeat}.json").write_text(redact(json.dumps(trace.to_dict(), default=str)))


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True).stdout.strip()
    except OSError:  # no git executable
        return ""


def record(run_dir: Path, args: list[str]) -> dict[str, Any]:
    info: dict[str, Any] = {
        "started": datetime.now(UTC).isoformat(timespec="seconds"),
        "toolkit_version": package_version(),
    }
    if commit := _git("rev-parse", "HEAD"):  # only inside a git checkout; an installed toolkit has none
        info |= {"commit": commit, "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
                 "dirty": bool(_git("status", "--porcelain", "--untracked-files=no"))}
    info |= {
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
    from pacds_eval import baseline, harness
    from pacds_eval.support_agent import agent

    hashes = {
        "pacds.system": sha256(agent_provider.AGENT_SYSTEM_PROMPT),
        "pacds.final": sha256(agent_provider.FINAL_INSTRUCTION),
        "pacds.schema_instruction": sha256(agent_provider.SCHEMA_INSTRUCTION),
        "pacds.premature_ready": sha256(agent_provider.PREMATURE_READY),
        "pacds.tools": sha256([*TOOL_DEFINITIONS, agent_provider.READY_TOOL]),
        "baseline.system": sha256(baseline.BASELINE_SYSTEM_PROMPT),
        "replay.question": sha256({"question": harness.QUESTION, "criteria": harness.CRITERIA}),
        "agent.tools": sha256([agent.CALL_PACDS_TOOL, agent.SUBMIT_DECISION_TOOL]),
        "agent.decision_note": sha256(agent.DECISION_NOTE),  # appended to the system prompt on claude_code
    }
    for skill in sorted(agent.SKILLS_DIR.glob("*/SKILL.md")):
        hashes[f"skill.{skill.parent.name}"] = sha256(skill.read_text())
    return hashes


def manifest(run_dir: Path, config_file: Path | None = None, target_version: str | None = None) -> dict[str, Any]:
    fields: dict[str, Any] = {"prompts": prompt_hashes(), "target_version": target_version}
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


def request_ids(run_dir: Path) -> set[str]:
    """Every PACDS request id the run's results files mention."""
    ids: set[str] = set()
    for path in run_dir.glob("*.json"):
        if path.name in SKIP:
            continue
        for row in json.loads(path.read_text()).get("results", []):
            ids.update(r["request_id"] for r in row.get("pacds_requests", []) if r.get("request_id"))
            if row.get("request_id"):
                ids.add(row["request_id"])
    return ids


def collect_traces(run_dir: Path, source: Path) -> int:
    """Copy the traces of this run's requests from a deployed PACDS's trace directory; returns how many."""
    target = run_dir / "traces" / "pacds"
    target.mkdir(parents=True, exist_ok=True)
    copied = 0
    for request_id in sorted(request_ids(run_dir)):
        trace = source / f"{request_id}.json"
        if trace.is_file():
            (target / trace.name).write_bytes(trace.read_bytes())
            copied += 1
    print(f"=== collected {copied} PACDS traces from {source}")
    return copied


def _archive() -> tuple[Any, str, str]:
    import boto3

    uri = os.environ.get(ARCHIVE_ENV)
    if not uri:
        sys.exit(f"{ARCHIVE_ENV} is not set")
    parts = urlsplit(uri)
    # Explicit keys: the generic AWS_* variables may belong to something else (e.g. an egress proxy).
    client = boto3.client("s3", region_name=os.environ.get("PACDS_EVAL_ARCHIVE_REGION"),
                          endpoint_url=os.environ.get("PACDS_EVAL_ARCHIVE_ENDPOINT") or None,
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


def sync(run_dir: Path) -> int:
    """Upload the run's files that are new or changed since the last sync, as <prefix><run>/<path>.

    eval.sh calls this every few minutes, so a run that dies with its container (no exit hook) keeps what it wrote.
    """
    client, bucket, prefix = _archive()
    state_file = run_dir / SYNC_STATE
    state = json.loads(state_file.read_text()) if state_file.is_file() else {}
    uploaded = 0
    for path in sorted(p for p in run_dir.rglob("*") if p.is_file() and p.name != SYNC_STATE):
        relative = path.relative_to(run_dir).as_posix()
        stat = path.stat()
        signature = [stat.st_size, stat.st_mtime_ns]
        if state.get(relative) == signature:
            continue
        client.upload_file(str(path), bucket, f"{prefix}{run_dir.name}/{relative}")
        state[relative] = signature
        uploaded += 1
    state_file.write_text(json.dumps(state))
    return uploaded


def fetch(name: str, dest: Path) -> Path:
    client, bucket, prefix = _archive()
    name = name.removesuffix(".tar.gz")
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tarball = Path(tmp) / f"{name}.tar.gz"
        try:
            client.download_file(bucket, f"{prefix}{name}.tar.gz", str(tarball))
        except Exception:  # noqa: BLE001 - no tarball: the run never finished; take its synced files
            keys = [item["Key"] for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"{prefix}{name}/")
                    for item in page.get("Contents", [])]
            if not keys:
                raise ValueError(f"no archived run named {name}") from None
            for key in keys:
                target = dest / name / key.removeprefix(f"{prefix}{name}/")
                target.parent.mkdir(parents=True, exist_ok=True)
                client.download_file(bucket, key, str(target))
            print(f"=== fetched {len(keys)} synced files (the run has no final archive): {dest / name}")
            return dest / name
        with tarfile.open(tarball) as tar:
            tar.extractall(dest, filter="data")
    print(f"=== fetched: {dest / name}")
    return dest / name


def list_archived() -> list[str]:
    """Archived runs: finished ones (<name>.tar.gz) and ones with synced files only (<name>/, marked 'partial')."""
    client, bucket, prefix = _archive()
    finished: dict[str, float] = {}
    synced: set[str] = set()
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
        for item in page.get("Contents", []):
            key = item["Key"].removeprefix(prefix)
            if key.endswith(".tar.gz"):
                finished[key.removesuffix(".tar.gz")] = item["Size"]
        synced.update(common["Prefix"].removeprefix(prefix).rstrip("/") for common in page.get("CommonPrefixes", []))
    names = sorted(set(finished) | (synced - {"_connectivity-check"}))
    for name in names:
        print(f"{name}  {finished[name] / 1e6:.1f} MB" if name in finished else f"{name}  partial (synced files only)")
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
        where = run_dir / "compose.log" if (run_dir / "compose.log").exists() else "the target PACDS's log (docker compose ... logs pacds)"
        print(f"    reasons: grep 'request=<request_id>' {where}")
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
    elif command == "collect-traces":
        collect_traces(Path(rest[0]), Path(rest[1]))
    elif command == "finalize":
        from pacds_eval.run import finalize

        finalize(Path(rest[0]))
    elif command == "sync":
        sync(Path(rest[0]))
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
