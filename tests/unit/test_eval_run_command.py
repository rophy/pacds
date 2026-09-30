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
    monkeypatch.setattr(run.runs, "manifest", lambda d, cfg=None, target_version=None: log.append(("manifest", cfg, target_version)))
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
    assert names(calls) == ["manifest"] and calls[0] == ("manifest", Path("c.json"), None)
    assert "NOTE" not in capsys.readouterr().out


def test_record_gets_the_original_argv(calls, tmp_path):
    argv = ["--target", "http://t", "--run-dir", str(tmp_path / "r"), "--no-seed", "--replay", "--set x"]
    run.main(argv)
    assert calls[0] == ("record", argv)


def test_exit_code_of_system_exit_none_is_zero():
    assert run.exit_code(SystemExit(None)) == 0 and run.exit_code(SystemExit("x")) == 1 and run.exit_code(SystemExit(5)) == 5


def test_finalize_survives_a_broken_console(calls, tmp_path, monkeypatch):
    monkeypatch.setenv("PACDS_EVAL_ARCHIVE_S3_URI", "s3://b")

    def broken(*a, **k):
        raise BrokenPipeError

    monkeypatch.setattr("builtins.print", broken)
    monkeypatch.setattr(run.runs, "collect_traces", lambda d, s: (_ for _ in ()).throw(RuntimeError("x")))
    run.finalize(tmp_path, "/src")
    assert names(calls) == ["errors", "finish", "analysis", "archive"]


def test_finalize_warns_when_the_report_fails(calls, tmp_path, capsys):
    calls.codes["pacds_eval.analysis"] = 1
    run.finalize(tmp_path)
    out = capsys.readouterr().out
    assert "WARNING: the report failed" in out and "=== report:" not in out


def test_run_step_quiet_silences_the_child(capfd):
    import sys as _sys

    cmd = "import sys; print('hi'); sys.exit(3)"
    assert run.subprocess.run([_sys.executable, "-c", cmd]).returncode == 3
    assert capfd.readouterr().out == "hi\n"
    real = run.subprocess.run
    seen = {}
    run.subprocess.run = lambda argv, stdout=None: seen.update(stdout=stdout) or real([_sys.executable, "-c", cmd], stdout=stdout)
    try:
        assert run.run_step("m", [], quiet=True) == 3
    finally:
        run.subprocess.run = real
    assert seen["stdout"] == run.subprocess.DEVNULL and capfd.readouterr().out == ""


def test_finalize_command_via_runs(calls, tmp_path, monkeypatch):
    from pacds_eval import cli

    assert cli.main(["runs", "finalize", str(tmp_path)]) == 0
    assert names(calls) == ["errors", "finish", "analysis"]


def test_tee_starts_in_its_own_session(monkeypatch, tmp_path):
    seen = {}

    class Fake:
        stdin = None

    def popen(cmd, **kw):
        seen.update(kw)
        raise RuntimeError("stop")

    monkeypatch.setattr(run.subprocess, "Popen", popen)
    with pytest.raises(RuntimeError):
        with run.tee_output(tmp_path / "l"):
            pass
    assert seen["start_new_session"] is True


def test_target_version_reaches_the_manifest_and_a_major_mismatch_warns(calls, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(run, "health", lambda url: '{"status": "ok", "version": "99.0.0"}')
    assert run.main(["--target", "http://t", "--run-dir", str(tmp_path / "r"), "--no-seed", "--replay", "--set x"]) == 0
    assert ("manifest", None, "99.0.0") in calls
    assert "differ in major version" in capsys.readouterr().out


def test_no_warning_when_the_target_reports_no_version(calls, tmp_path, capsys):
    assert run.main(["--target", "http://t", "--run-dir", str(tmp_path / "r"), "--no-seed", "--replay", "--set x"]) == 0
    assert ("manifest", None, None) in calls and "differ in major" not in capsys.readouterr().out
