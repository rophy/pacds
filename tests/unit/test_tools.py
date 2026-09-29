import subprocess

import pytest

from pacds.engine.tools import WorkspaceTools


@pytest.fixture
def tools(tmp_path) -> WorkspaceTools:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "checkout.py").write_text("def pay():\n    raise ValueError('payment declined')\n")
    (repo / "README.md").write_text("docs\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    (repo / "escape").symlink_to("/etc")
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "server.log").write_text("\n".join(f"line {i}" for i in range(1, 11)) + "\nERROR payment declined\n")
    (tmp_path / "secret.txt").write_text("outside")
    return WorkspaceTools(repo, logs)


def test_definitions_name_all_tools(tools):
    names = {tool["function"]["name"] for tool in tools.definitions}
    assert names == {"search_code", "read_file", "list_files", "git_log", "git_show", "search_logs", "read_log"}


async def test_search_code(tools):
    result = await tools.call("search_code", {"pattern": "payment declined"})
    assert "src/checkout.py:2:" in result
    assert await tools.call("search_code", {"pattern": "nothing-matches-this"}) == "no matches"
    assert "checkout.py" in await tools.call("search_code", {"pattern": "def", "path_glob": "src/*.py"})


async def test_search_code_pattern_starting_with_dash(tools):
    assert not (await tools.call("search_code", {"pattern": "--help"})).startswith("usage")


async def test_read_file_with_range(tools):
    assert await tools.call("read_file", {"path": "src/checkout.py", "start_line": 2, "end_line": 2}) == (
        "2:     raise ValueError('payment declined')"
    )


async def test_list_files_hides_git_dir(tools):
    listing = await tools.call("list_files", {})
    assert "src/" in listing and "README.md" in listing and ".git" not in listing


async def test_search_and_read_logs(tools):
    assert "server.log:11:ERROR payment declined" in await tools.call("search_logs", {"pattern": "ERROR"})
    assert await tools.call("read_log", {"name": "server.log", "start_line": 10, "end_line": 10}) == "10: line 10"


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("read_file", {"path": "../secret.txt"}),
        ("read_file", {"path": "/etc/passwd"}),
        ("read_file", {"path": "escape/passwd"}),
        ("read_file", {"path": ".git/config"}),
        ("list_files", {"path": ".."}),
        ("read_log", {"name": "../secret.txt"}),
        ("search_logs", {"pattern": "x", "name": "../secret.txt"}),
    ],
)
async def test_paths_outside_workspace_are_refused(tools, name, arguments):
    assert (await tools.call(name, arguments)).startswith("error:")


async def test_bad_calls_return_errors(tools):
    assert (await tools.call("rm_rf", {})).startswith("error:")
    assert (await tools.call("read_file", {"nope": 1})).startswith("error:")
    assert (await tools.call("search_code", {"pattern": "("})).startswith("error:")


def _git(repo, *args):
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
           "GIT_AUTHOR_DATE": "2025-01-02T00:00:00Z", "GIT_COMMITTER_DATE": "2025-01-02T00:00:00Z", "PATH": "/usr/bin:/bin"}
    return subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def history(tmp_path) -> WorkspaceTools:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "flush.py").write_text("def flush(offset):\n    commit(offset)\n")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "Add offset flushing")
    (repo / "src" / "flush.py").write_text("def flush(offset, record):\n    if record.sent:\n        commit(offset)\n")
    (repo / "README.md").write_text("docs\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "Skip work for filtered records")
    (tmp_path / "logs").mkdir()
    return WorkspaceTools(repo, tmp_path / "logs")


async def test_git_log_lists_commits_newest_first_and_by_path(history):
    log = await history.call("git_log", {})
    assert [line.split(" ", 2)[2] for line in log.splitlines()] == ["Skip work for filtered records", "Add offset flushing"]
    assert "2025-01-02" in log
    assert (await history.call("git_log", {"path": "README.md"})).endswith("Skip work for filtered records")
    assert len((await history.call("git_log", {"max_count": 1})).splitlines()) == 1


async def test_git_show_returns_message_and_diff(history):
    commit = (await history.call("git_log", {"max_count": 1})).split()[0]
    shown = await history.call("git_show", {"commit": commit, "path": "src/flush.py"})
    assert "Skip work for filtered records" in shown and "+    if record.sent:" in shown and "README" not in shown


@pytest.mark.parametrize("arguments", [{"commit": "HEAD"}, {"commit": "--output=/tmp/x"}, {"commit": "abc"}])
async def test_git_show_accepts_only_commit_ids(history, arguments):
    assert (await history.call("git_show", arguments)).startswith("error: commit must be a commit id")


async def test_git_history_paths_stay_inside_the_repository(history):
    assert (await history.call("git_log", {"path": "../secret.txt"})) == "error: path is outside the workspace"
    assert (await history.call("git_log", {"path": ".git/config"})) == "error: path is outside the workspace"
    assert (await history.call("git_show", {"commit": "0000000"})) == "error: unknown commit or path"


async def test_fingerprint_names_commit_and_log_digests(history):
    import hashlib

    logs = history._logs
    (logs / "server.log").write_text("line")
    head = _git(history._repo, "rev-parse", "HEAD")
    assert await history.fingerprint() == {"commit": head, "logs": {"server.log": hashlib.sha256(b"line").hexdigest()}}


async def test_fingerprint_hashes_logs_off_the_event_loop(history, monkeypatch):
    import asyncio
    import time

    from pacds.engine import tools as module

    (history._logs / "server.log").write_text("line")
    real = module._file_digest

    def slow(path):
        time.sleep(0.3)  # a large log
        return real(path)
    monkeypatch.setattr(module, "_file_digest", slow)
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1
    task = asyncio.create_task(ticker())
    fingerprint = await history.fingerprint()
    task.cancel()
    assert fingerprint["logs"]["server.log"] == real(history._logs / "server.log")
    assert ticks >= 5  # the loop kept running while the log was hashed
