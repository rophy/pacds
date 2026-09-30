from pathlib import Path

from pacds_eval import paths


def clear(monkeypatch):
    for var in ("PACDS_RUNS_DIR", "EVAL_RUN_DIR", "PACDS_TRACE_SOURCE_DIR"):
        monkeypatch.delenv(var, raising=False)


def test_runs_root_defaults_to_eval_runs_and_follows_the_variable(monkeypatch):
    clear(monkeypatch)
    assert paths.runs_root() == Path("eval-runs")
    monkeypatch.setenv("PACDS_RUNS_DIR", "/runs")
    assert paths.runs_root() == Path("/runs")


def test_default_run_dir_order(monkeypatch):
    clear(monkeypatch)
    monkeypatch.setenv("PACDS_RUNS_DIR", "/runs")
    default = paths.default_run_dir()
    assert default.parent == Path("/runs") and len(default.name) == 16 and default.name.endswith("Z")
    monkeypatch.setenv("EVAL_RUN_DIR", "/env")
    assert paths.default_run_dir() == Path("/env")
    assert paths.default_run_dir("/flag") == Path("/flag")


def test_trace_source_uses_the_mount_only_when_it_is_a_directory(monkeypatch, tmp_path):
    clear(monkeypatch)
    monkeypatch.setattr(paths, "TRACES_MOUNT", tmp_path / "traces")
    assert paths.trace_source_dir() is None
    (tmp_path / "traces").mkdir()
    assert paths.trace_source_dir() == str(tmp_path / "traces")
    monkeypatch.setenv("PACDS_TRACE_SOURCE_DIR", "/elsewhere")
    assert paths.trace_source_dir() == "/elsewhere"


def test_no_stale_module_hints_in_user_facing_text():
    import re

    root = Path(__file__).parents[2] / "src"
    stale = re.compile(r"python -m (pacds_eval\.(analysis|runs|catalog|casebook|harness|support_agent)|pacds\.devtools)")
    hits = [f"{p.relative_to(root)}:{n}" for p in root.rglob("*.py") for n, line in enumerate(p.read_text().splitlines(), 1) if stale.search(line)]
    assert hits == []
