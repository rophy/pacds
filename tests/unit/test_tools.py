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
    assert names == {"search_code", "read_file", "list_files", "search_logs", "read_log"}


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
