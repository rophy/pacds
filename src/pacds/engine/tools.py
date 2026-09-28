"""Read-only tools the agent uses to inspect the repository checkout, its recent history, and log files."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

MAX_MATCHES = 100
MAX_LINE_CHARS = 400
MAX_READ_LINES = 400
MAX_LIST_ENTRIES = 500
SEARCH_TIMEOUT_SECONDS = 15
MAX_LOG_COMMITS = 50
MAX_SHOW_LINES = 400
_COMMIT = re.compile(r"[0-9a-f]{7,40}")
# History commands run on the local checkout only: never fetch missing objects over the network.
_GIT = ["git", "-c", "protocol.allow=never", "-c", "core.pager=cat", "--no-pager"]


class ToolError(Exception):
    """A tool call the agent should see as an error message."""


def _function(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False},
        },
    }


_LINE_RANGE = {
    "start_line": {"type": "integer", "minimum": 1, "description": "First line to read (default 1)"},
    "end_line": {"type": "integer", "minimum": 1, "description": f"Last line to read (at most {MAX_READ_LINES} lines per call)"},
}

TOOL_DEFINITIONS = [
    _function(
        "search_code",
        f"Search the source code with an extended regular expression. Returns up to {MAX_MATCHES} lines as path:line:text.",
        {
            "pattern": {"type": "string", "description": "Extended regular expression"},
            "path_glob": {"type": "string", "description": "Optional glob limiting the files searched, e.g. '**/*.go'"},
        },
        ["pattern"],
    ),
    _function(
        "read_file",
        "Read lines from a source file.",
        {"path": {"type": "string", "description": "Path relative to the repository root"}, **_LINE_RANGE},
        ["path"],
    ),
    _function(
        "list_files",
        "List the entries of a source directory. Directories end with '/'.",
        {"path": {"type": "string", "description": "Directory relative to the repository root (default '.')"}},
        [],
    ),
    _function(
        "git_log",
        f"List recent commits before the deployed version, newest first (at most {MAX_LOG_COMMITS}), as "
        "'<commit> <date> <subject>'. Only history up to the deployed version exists; the oldest commit shown may be "
        "where the available history starts.",
        {
            "path": {"type": "string", "description": "Optional file or directory: only commits that changed it"},
            "max_count": {"type": "integer", "minimum": 1, "maximum": MAX_LOG_COMMITS, "description": "Default 20"},
        },
        [],
    ),
    _function(
        "git_show",
        f"Show one commit from git_log: its message and diff (at most {MAX_SHOW_LINES} lines), optionally limited to one path.",
        {
            "commit": {"type": "string", "description": "Commit id from git_log"},
            "path": {"type": "string", "description": "Optional file or directory to limit the diff to"},
        },
        ["commit"],
    ),
    _function(
        "search_logs",
        f"Search the attached log files with an extended regular expression. Returns up to {MAX_MATCHES} lines as name:line:text.",
        {
            "pattern": {"type": "string", "description": "Extended regular expression"},
            "name": {"type": "string", "description": "Optional log file name to search"},
        },
        ["pattern"],
    ),
    _function(
        "read_log",
        "Read lines from an attached log file.",
        {"name": {"type": "string", "description": "Log file name"}, **_LINE_RANGE},
        ["name"],
    ),
]


class WorkspaceTools:
    def __init__(self, repo_dir: Path, logs_dir: Path) -> None:
        self._repo = repo_dir.resolve()
        self._logs = logs_dir.resolve()

    @property
    def definitions(self) -> list[dict[str, Any]]:
        return TOOL_DEFINITIONS

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        handlers: dict[str, Callable[..., Awaitable[str]]] = {
            "search_code": self.search_code,
            "read_file": self.read_file,
            "list_files": self.list_files,
            "git_log": self.git_log,
            "git_show": self.git_show,
            "search_logs": self.search_logs,
            "read_log": self.read_log,
        }
        handler = handlers.get(name)
        if handler is None:
            return f"error: unknown tool {name}"
        try:
            return await handler(**arguments)
        except TypeError:
            return "error: invalid arguments"
        except ToolError as error:
            return f"error: {error}"

    async def search_code(self, pattern: str, path_glob: str | None = None) -> str:
        pathspec = f":(glob){path_glob}" if path_glob else "."
        lines = await _search(["git", "-C", str(self._repo), "grep", "-n", "-I", "-E", "-e", pattern, "--", pathspec], self._repo)
        return _format(lines)

    async def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        return _read(self._inside(self._repo, path), start_line, end_line)

    async def list_files(self, path: str = ".") -> str:
        directory = self._inside(self._repo, path)
        if not directory.is_dir():
            raise ToolError("not a directory")
        entries = sorted(entry for entry in directory.iterdir() if entry.name != ".git")
        names = [entry.name + ("/" if entry.is_dir() else "") for entry in entries[:MAX_LIST_ENTRIES]]
        return "\n".join(names) or "(empty directory)"

    async def git_log(self, path: str | None = None, max_count: int = 20) -> str:
        count = min(max(1, int(max_count)), MAX_LOG_COMMITS)
        pathspec = [self._repo_path(path)] if path else []
        lines = await _run([*_GIT, "-C", str(self._repo), "log", f"--max-count={count}", "--date=short",
                            "--format=%h %ad %s", "--", *pathspec], self._repo)
        return _format(lines[:MAX_LOG_COMMITS]) if lines else "no commits"

    async def git_show(self, commit: str, path: str | None = None) -> str:
        if not _COMMIT.fullmatch(commit.lower()):
            raise ToolError("commit must be a commit id from git_log")
        pathspec = [self._repo_path(path)] if path else []
        lines = await _run([*_GIT, "-C", str(self._repo), "show", "--no-color", "--date=short", "--format=%h %ad %s%n%n%b",
                            commit.lower(), "--", *pathspec], self._repo)
        shown = [line[:MAX_LINE_CHARS] for line in lines[:MAX_SHOW_LINES]]
        if len(lines) > MAX_SHOW_LINES:
            shown.append(f"... ({len(lines) - MAX_SHOW_LINES} more lines; limit the diff with path)")
        return "\n".join(shown) or "(empty commit)"

    def _repo_path(self, relative: str) -> str:
        # Like _inside, but the path may no longer exist at the deployed version (deleted or renamed files).
        candidate = (self._repo / relative).resolve()
        if candidate != self._repo and self._repo not in candidate.parents:
            raise ToolError("path is outside the workspace")
        if ".git" in candidate.relative_to(self._repo).parts:
            raise ToolError("path is outside the workspace")
        return candidate.relative_to(self._repo).as_posix() or "."

    async def search_logs(self, pattern: str, name: str | None = None) -> str:
        target = self._inside(self._logs, name).name if name else "."
        lines = await _search(["grep", "-r", "-n", "-I", "-E", "-e", pattern, "--", target], self._logs)
        return _format([line.removeprefix("./") for line in lines])

    async def read_log(self, name: str, start_line: int = 1, end_line: int | None = None) -> str:
        return _read(self._inside(self._logs, name), start_line, end_line)

    def _inside(self, root: Path, relative: str) -> Path:
        candidate = (root / relative).resolve()
        if candidate != root and root not in candidate.parents:
            raise ToolError("path is outside the workspace")
        if ".git" in candidate.relative_to(root).parts:
            raise ToolError("path is outside the workspace")
        if not candidate.exists():
            raise ToolError("no such file or directory")
        return candidate


async def _run(command: list[str], cwd: Path) -> list[str]:
    process = await asyncio.create_subprocess_exec(
        *command, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=SEARCH_TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise ToolError("git timed out") from None
    if process.returncode != 0:
        raise ToolError("unknown commit or path")
    return stdout.decode(errors="replace").splitlines()


async def _search(command: list[str], cwd: Path) -> list[str]:
    process = await asyncio.create_subprocess_exec(
        *command, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=SEARCH_TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise ToolError("search timed out") from None
    if process.returncode == 1:
        return []
    if process.returncode != 0:
        raise ToolError("invalid search pattern")
    return stdout.decode(errors="replace").splitlines()[:MAX_MATCHES]


def _format(lines: list[str]) -> str:
    return "\n".join(line[:MAX_LINE_CHARS] for line in lines) or "no matches"


def _read(path: Path, start_line: int, end_line: int | None) -> str:
    if not path.is_file():
        raise ToolError("not a file")
    start = max(1, start_line)
    last = start + MAX_READ_LINES - 1
    end = min(end_line, last) if end_line is not None else last
    lines = []
    with path.open(errors="replace") as handle:
        for number, line in enumerate(handle, start=1):
            if number < start:
                continue
            if number > end:
                break
            lines.append(f"{number}: {line.rstrip()[:MAX_LINE_CHARS]}")
    return "\n".join(lines) or "(no lines in range)"
