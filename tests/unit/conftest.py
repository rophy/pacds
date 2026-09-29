import sys
from pathlib import Path

import pytest

from pacds.engine import claude_code

FAKE = Path(__file__).parent / "fake_claude.py"


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv(claude_code.EXECUTABLE_ENV, f"{sys.executable} {FAKE}")
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # the fake writes transcripts of persisted sessions under $HOME

    def use(stream: str, tool: str | None = None) -> Path:
        monkeypatch.setenv("FAKE_CLAUDE_STREAM", stream)
        if tool:
            monkeypatch.setenv("FAKE_CLAUDE_CALL_TOOL", tool)
        return log
    return use
