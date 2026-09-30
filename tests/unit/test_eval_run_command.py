import contextlib
import json
import threading
from pathlib import Path

import pytest

from pacds_eval import run


class Calls(list):
    codes: dict


@pytest.fixture
def calls(monkeypatch, tmp_path):
    log = Calls()
    monkeypatch.setattr(run, "tee_output", lambda path: contextlib.nullcontext())
    monkeypatch.setattr(run, "health", lambda url: "ok")
    monkeypatch.setattr(run, "seed", lambda: log.append(("seed",)))
    monkeypatch.setattr(run.runs, "record", lambda d, argv: log.append(("record", list(argv))))
    monkeypatch.setattr(run.runs, "manifest", lambda d, cfg=None: log.append(("manifest", cfg)))
    monkeypatch.setattr(run.runs, "collect_traces", lambda d, src: log.append(("collect", src)))
    monkeypatch.setattr(run.runs, "errors", lambda d: log.append(("errors",)))
    monkeypatch.setattr(run.runs, "finish", lambda d: log.append(("finish",)))
    monkeypatch.setattr(run.runs, "archive", lambda d: log.append(("archive",)))
    monkeypatch.setattr(run.runs, "sync", lambda d: log.append(("sync",)))
    codes = {}

    def step(module, argv, quiet=False):
        log.append(("step", module, argv))
        return codes.get(module, 0)

    monkeypatch.setattr(run, "run_step", step)
    for var in ("EVAL_RUN_DIR", "PACDS_TRACE_SOURCE_DIR", "PACDS_EVAL_ARCHIVE_S3_URI", "EVAL_SYNC_SECONDS"):
        monkeypatch.delenv(var, raising=False)
    log.codes = codes
    return log


def names(log):
    return [e[0] if e[0] != "step" else e[1].split(".")[-1] if e[1] != "pacds_eval.support_agent.run" else "support" for e in log]


def test_steps_append_out_and_trace_dir_only_when_absent(calls, tmp_path):
    rd = tmp_path / "r"
    assert run.main(["--target", "http://t", "--run-dir", str(rd), "--no-seed", "--replay", "--set hard --out x.json",
                     "--replay", "--set clear", "--support", "--variant full"]) == 0
    steps = [e[2] for e in calls if e[0] == "step" and e[1] != "pacds_eval.analysis"]
    assert steps[0] == ["--set", "hard", "--out", "x.json", "--trace-dir", f"{rd}/traces/replay-1"]
    assert steps[1] == ["--set", "clear", "--out", f"{rd}/replay-2.json", "--trace-dir", f"{rd}/traces/replay-2"]
    assert steps[2] == ["--variant", "full", "--out", f"{rd}/support-1.json", "--trace-dir", f"{rd}/traces/support-1"]
    assert (rd / "traces").is_dir()


def test_replay_from_is_appended_to_every_step(calls, tmp_path, capsys):
    rd = tmp_path / "r"
    run.main(["--target", "http://t", "--run-dir", str(rd), "--replay-from", "a,b", "--replay", "--set x", "--support", ""])
    steps = [e[2] for e in calls if e[0] == "step" and e[1] != "pacds_eval.analysis"]
    assert all(s[-2:] == ["--replay-from", "a,b"] for s in steps)
    assert "PACDS replays only if the target was started" in capsys.readouterr().out


def test_order_and_finally_steps(calls, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("PACDS_TRACE_SOURCE_DIR", "/src")
    monkeypatch.setenv("PACDS_EVAL_ARCHIVE_S3_URI", "s3://b/p")
    monkeypatch.setenv("EVAL_SYNC_SECONDS", "1000")
    assert run.main(["--target", "http://t", "--run-dir", str(tmp_path / "r"), "--replay", "--set x"]) == 0
    assert names(calls) == ["record", "seed", "manifest", "harness", "collect", "errors", "finish", "analysis", "archive"]
    out = capsys.readouterr().out
    assert "=== target: http://t (ok)" in out and f"=== report: {tmp_path}/r/report/report.md" in out
    assert "WARNING: no --pacds-config" in out


def test_failing_step_stops_run_but_finally_steps_run(calls, tmp_path):
    calls.codes["pacds_eval.harness"] = 3
    status = run.main(["--target", "http://t", "--run-dir", str(tmp_path / "r"), "--no-seed", "--replay", "--set x",
                       "--support", "--variant y"])
    assert status == 3
    assert names(calls) == ["record", "manifest", "harness", "errors", "finish", "analysis"]


def test_seed_failure_sets_status_and_still_finishes(calls, tmp_path, monkeypatch):
    def boom():
        raise SystemExit(4)

    monkeypatch.setattr(run, "seed", boom)
    assert run.main(["--target", "http://t", "--run-dir", str(tmp_path / "r"), "--replay", "--set x"]) == 4
    assert names(calls) == ["record", "errors", "finish", "analysis"]


def test_run_dir_default_and_env(calls, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run.main(["--target", "http://t", "--no-seed"])
    (created,) = list((tmp_path / "eval-runs").iterdir())
    assert len(created.name) == 16 and created.name.endswith("Z")
    monkeypatch.setenv("EVAL_RUN_DIR", str(tmp_path / "env"))
    run.main(["--target", "http://t", "--no-seed"])
    assert (tmp_path / "env" / "traces").is_dir()


def test_sync_thread_only_with_archive_variable(calls, tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(threading.Thread, "start", lambda self: started.append(self))
    run.main(["--target", "http://t", "--run-dir", str(tmp_path / "a"), "--no-seed"])
    assert started == []
    monkeypatch.setenv("PACDS_EVAL_ARCHIVE_S3_URI", "s3://b")
    run.main(["--target", "http://t", "--run-dir", str(tmp_path / "b"), "--no-seed"])
    assert len(started) == 1


def test_sync_loop_warns_on_failure(monkeypatch, tmp_path, capsys):
    syncer = run.Syncer(tmp_path)
    monkeypatch.setenv("EVAL_SYNC_SECONDS", "0")
    monkeypatch.setattr(run.runs, "sync", lambda d: (syncer.stop(), 1 / 0))
    syncer.loop()
    assert "WARNING: syncing the run to S3 failed" in capsys.readouterr().out


def test_env_for_steps_and_restored(calls, tmp_path, monkeypatch):
    import os

    monkeypatch.delenv("PACDS_URL", raising=False)
    monkeypatch.delenv("PACDS_CASES_DIR", raising=False)
    seen = {}
    monkeypatch.setattr(run, "seed", lambda: seen.update(url=os.environ["PACDS_URL"], cases=os.environ["PACDS_CASES_DIR"]))
    run.main(["--target", "http://t", "--cases-dir", "cd", "--run-dir", str(tmp_path / "r")])
    assert seen == {"url": "http://t", "cases": "cd"}
    assert "PACDS_URL" not in os.environ and "PACDS_CASES_DIR" not in os.environ


def test_stack_mode_skips_finalize_and_note(calls, tmp_path, capsys):
    run.main(["--target", "http://localhost:3002", "--run-dir", str(tmp_path / "r"), "--no-seed", "--stack",
              "--replay-from", "x", "--pacds-config", "c.json"])
    assert names(calls) == ["record", "manifest"] and calls[1] == ("manifest", Path("c.json"))
    assert "NOTE" not in capsys.readouterr().out


def test_record_gets_the_original_argv(calls, tmp_path):
    argv = ["--target", "http://t", "--run-dir", str(tmp_path / "r"), "--no-seed", "--replay", "--set x"]
    run.main(argv)
    assert calls[0] == ("record", argv)
