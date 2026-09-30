import subprocess
import sys

import pytest

from pacds import cli


def test_no_arguments_prints_help(capsys):
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    for name in ("serve", "check-llm", "eval"):
        assert name in out


def test_unknown_command_is_an_error(capsys):
    assert cli.main(["bogus"]) == 2
    assert "serve" in capsys.readouterr().err


def test_eval_lists_its_subcommands(capsys):
    assert cli.main(["eval"]) == 0
    out = capsys.readouterr().out
    for name in ("run", "audit", "casebook", "report", "catalog", "runs", "replay", "support"):
        assert name in out


def test_serve_calls_the_service(monkeypatch):
    called = []
    monkeypatch.setattr("pacds.main.main", lambda: called.append(True))
    assert cli.main(["serve"]) == 0
    assert called == [True]


def test_serve_rejects_arguments(monkeypatch):
    monkeypatch.setattr("pacds.main.main", lambda: pytest.fail("started"))
    assert cli.main(["serve", "--port", "1"]) == 2


def test_check_llm_passes_arguments_through(monkeypatch):
    seen = []
    monkeypatch.setattr("pacds.devtools.check_llm.main", lambda: seen.append(sys.argv[:]))
    assert cli.main(["check-llm", "cfg.yaml"]) == 0
    assert seen == [["pacds check-llm", "cfg.yaml"]]


def test_eval_report_help_uses_the_pacds_prog(capsys):
    with pytest.raises(SystemExit) as exit_:
        cli.main(["eval", "report", "--help"])
    assert exit_.value.code == 0
    assert capsys.readouterr().out.startswith("usage: pacds eval report")


def test_eval_passes_argv_and_restores_it(monkeypatch):
    seen = []
    monkeypatch.setattr("pacds_eval.runs.main", lambda: seen.append(sys.argv[:]))
    before = sys.argv[:]
    assert cli.main(["eval", "runs", "list", "--x"]) == 0
    assert seen == [["pacds eval runs", "list", "--x"]]
    assert sys.argv == before


def test_eval_unknown_command():
    assert cli.main(["eval", "bogus"]) == 2


def test_command_runs_and_the_service_import_stays_clean():
    result = subprocess.run([sys.executable, "-m", "pacds.cli"], capture_output=True, text=True)
    assert result.returncode == 0
    check = "import pacds.cli, sys; assert 'pacds_eval' not in sys.modules"
    assert subprocess.run([sys.executable, "-c", check]).returncode == 0
    assert subprocess.run(["uv", "run", "--offline", "pacds"], capture_output=True).returncode == 0
