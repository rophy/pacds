"""`pacds eval run`: evaluate a PACDS instance (a deployed one, or the Compose stack scripts/eval.sh started).

Creates the run directory, records the run, seeds the case set's logs, runs each --replay / --support step, and always
finishes the run (traces, errors, results summary, report, archive), pass or fail. The exit status is the first failing
step's. See docs/evaluation-runbook.md.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import subprocess
import sys
import threading
import urllib.request
from collections.abc import Iterator
from pathlib import Path

from pacds.version import package_version
from pacds_eval import runs
from pacds_eval.paths import default_run_dir, trace_source_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pacds eval run", description="Evaluate a deployed PACDS")
    parser.add_argument("--target", required=True, help="base URL of the PACDS to evaluate")
    parser.add_argument("--cases-dir", help="the case set for every step (sets PACDS_CASES_DIR)")
    parser.add_argument("--pacds-config", help="the target's configuration (`pacds show-config` in its container), recorded in run.json")
    parser.add_argument("--replay-from", help="RUN[,RUN...]: answer identical model requests with the recorded response")
    parser.add_argument("--no-seed", action="store_true", help="do not seed the case set's logs")
    parser.add_argument("--replay", action="append", default=[], metavar="ARGS", help="a replay harness step (repeatable)")
    parser.add_argument("--support", action="append", default=[], metavar="ARGS", help="a support agent step (repeatable)")
    parser.add_argument("--run-dir", help="run directory (default $EVAL_RUN_DIR, else $PACDS_RUNS_DIR or eval-runs, then <UTC time>)")
    # scripts/eval.sh runs this against its Compose stack and owns the run around it, so --stack means: do not tee the
    # console (eval.sh does), do not record the run (eval.sh recorded it before starting the stack), do not print the
    # --replay-from NOTE (the stack does replay), and do not finalize (eval.sh runs `pacds eval runs finalize` after
    # saving the stack's logs, so the report and the archive include compose.log).
    parser.add_argument("--stack", action="store_true", help=argparse.SUPPRESS)
    return parser


def step_args(args: str, name: str, run_dir: Path, replay_from: str | None) -> list[str]:
    """The step's arguments: results and client traces go to the run directory unless the caller passed them."""
    argv = shlex.split(args)
    if "--out" not in argv:
        argv += ["--out", str(run_dir / f"{name}.json")]
    if "--trace-dir" not in argv:
        argv += ["--trace-dir", str(run_dir / "traces" / name)]
    if replay_from:
        argv += ["--replay-from", replay_from]
    return argv


def run_step(module: str, argv: list[str], quiet: bool = False) -> int:
    """One runner in its own process: it isolates sys.argv, event loops and module state."""
    out = subprocess.DEVNULL if quiet else None
    return subprocess.run([sys.executable, "-m", module, *argv], stdout=out).returncode


def health(url: str) -> str:
    try:
        with urllib.request.urlopen(f"{url}/healthz", timeout=30) as response:
            return response.read().decode(errors="replace")
    except Exception:
        return "health check failed"


def target_version(body: str) -> str | None:
    """The `version` of a /healthz body; None when the target is unreachable or too old to report one."""
    try:
        version = json.loads(body).get("version")
    except (ValueError, AttributeError):
        return None
    return version if isinstance(version, str) else None


def version_warning(toolkit: str, target: str | None) -> str | None:
    major = lambda v: v.removeprefix("v").split(".")[0]  # noqa: E731
    if target and "unknown" not in (toolkit, target) and major(toolkit) != major(target):
        return f"WARNING: toolkit {toolkit} and target {target} differ in major version"
    return None


@contextlib.contextmanager
def tee_output(log: Path) -> Iterator[None]:
    """Console output (this process and its children) also goes to `log`, like `exec > >(tee -a log) 2>&1`."""
    sys.stdout.flush()
    sys.stderr.flush()
    saved = os.dup(1), os.dup(2)
    # Own session: Ctrl-C must not kill tee, or the steps that finish the run would have no console.
    tee = subprocess.Popen(["tee", "-a", str(log)], stdin=subprocess.PIPE, start_new_session=True)
    assert tee.stdin is not None
    os.dup2(tee.stdin.fileno(), 1)
    os.dup2(tee.stdin.fileno(), 2)
    try:
        yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(saved[0], 1)
        os.dup2(saved[1], 2)
        for fd in saved:
            os.close(fd)
        tee.stdin.close()
        tee.wait()


class Syncer:
    """Uploads the run's new files every EVAL_SYNC_SECONDS, so a run lost with its container keeps what it wrote."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def loop(self) -> None:
        interval = float(os.environ.get("EVAL_SYNC_SECONDS") or 300)
        while not self.stopped.wait(interval):
            try:
                runs.sync(self.run_dir)
            except Exception:
                print("WARNING: syncing the run to S3 failed")

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stopped.set()
        if self.thread.is_alive():
            self.thread.join(timeout=10)


def say(text: str, **kwargs) -> None:
    """print that a broken console (a dead pipe after Ctrl-C) cannot turn into a failure of the steps after it."""
    try:
        print(text, **kwargs)
    except OSError:
        pass


def finalize(run_dir: Path, trace_source: str | None = None) -> None:
    """The steps that run whether or not the evaluation passed. None of them changes the exit status."""
    if trace_source:
        try:
            runs.collect_traces(run_dir, Path(trace_source))
        except Exception:
            say("WARNING: collecting PACDS traces failed")
    for name, function in (("errors", runs.errors), ("finish", runs.finish)):
        try:
            function(run_dir)
        except Exception as exc:
            say(f"WARNING: runs {name} failed: {exc!r}", file=sys.stderr)
    if run_step("pacds_eval.analysis", ["report", str(run_dir)], quiet=True) == 0:
        say(f"=== report: {run_dir}/report/report.md")
    else:
        say(f"WARNING: the report failed; rerun pacds eval report {run_dir}")
    if os.environ.get("PACDS_EVAL_ARCHIVE_S3_URI"):
        try:
            runs.archive(run_dir)
        except Exception:
            say(f"WARNING: archiving the run failed; it is only in {run_dir}")


def seed() -> None:
    from pacds_eval.harness import resolve_cases_dir
    from pacds_eval.s3 import LogStore

    store = LogStore.from_env()
    count = store.seed([resolve_cases_dir()])
    print(f"=== seeded {count} log files to s3://{store.bucket}/replay/ at {store.upload_endpoint or store.endpoint}")


def exit_code(exc: BaseException) -> int:
    if isinstance(exc, SystemExit):
        if exc.code is None:
            return 0
        return exc.code if isinstance(exc.code, int) else 1
    return 1


def evaluate(args: argparse.Namespace, run_dir: Path) -> int:
    status = 0
    syncer = None
    try:
        body = health(args.target)
        print(f"=== target: {args.target} ({body})")
        target = target_version(body)
        if warning := version_warning(package_version(), target):
            print(warning)
        if not args.no_seed:
            seed()
        if not args.pacds_config:
            print("WARNING: no --pacds-config; run.json will not record the target's model")
        runs.manifest(run_dir, Path(args.pacds_config) if args.pacds_config else None, target)
        if os.environ.get("PACDS_EVAL_ARCHIVE_S3_URI"):
            syncer = Syncer(run_dir)
            syncer.start()
        for module, label, name, steps in (
            ("pacds_eval.harness", "replay", "replay", args.replay),
            ("pacds_eval.support_agent.run", "support agent", "support", args.support),
        ):
            for n, step in enumerate(steps, 1):
                step_argv = step_args(step, f"{name}-{n}", run_dir, args.replay_from)
                print(f"=== {label}: {' '.join(step_argv)}")
                code = run_step(module, step_argv)
                if code:
                    return code
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt):
            raise
        if not isinstance(exc, SystemExit):
            import traceback

            traceback.print_exc()
        status = exit_code(exc)
    finally:
        if syncer:
            syncer.stop()
        if not args.stack:
            finalize(run_dir, trace_source_dir())
    return status


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    if args.replay_from and not args.stack:
        print("NOTE: --replay-from with --target replays the clients' calls only; PACDS replays only if the target was started")
        print("      with those recordings (trace.replay_from).")
    run_dir = default_run_dir(args.run_dir)
    (run_dir / "traces").mkdir(parents=True, exist_ok=True)
    saved = {key: os.environ.get(key) for key in ("PACDS_URL", "PACDS_CASES_DIR")}
    os.environ["PACDS_URL"] = args.target
    if args.cases_dir:
        os.environ["PACDS_CASES_DIR"] = args.cases_dir
    try:
        with contextlib.nullcontext() if args.stack else tee_output(run_dir / "eval.log"):
            print(f"=== run directory: {run_dir}")
            if not args.stack:
                runs.record(run_dir, argv)
            return evaluate(args, run_dir)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == "__main__":
    sys.exit(main())
