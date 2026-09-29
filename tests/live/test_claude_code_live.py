"""Live checks of the Claude Code backend against the real CLI (haiku). Run: uv run pytest -m claude_code tests/live"""

import pytest

from pacds.engine import claude_code
from pacds.engine.mcp_http import Toolset

pytestmark = pytest.mark.claude_code
SCHEMA = {"type": "object", "properties": {"retries": {"type": "integer", "description": "The value of MAX_RETRIES in the file"}}, "required": ["retries"], "additionalProperties": False}
DEFS = [{"type": "function", "function": {"name": "read_file", "description": "Read a file from the repository.",
                                          "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}]


async def test_http_mcp_tool_and_schema_answer():
    calls = []

    async def call(name: str, arguments: dict) -> str:
        calls.append((name, arguments))
        return "MAX_RETRIES = 3\n\ndef retry():\n    # tries at most MAX_RETRIES times\n"
    result = await claude_code.run(system="You investigate code with the given tools. Always read the file before answering.",
                                   prompt="What is MAX_RETRIES in retry.py?", schema=SCHEMA, model="haiku",
                                   max_turns=5, toolset=Toolset(DEFS, call))
    assert calls and calls[0][0] == "read_file"
    assert result.structured_output == {"retries": 3}
    assert result.usage["input"] > 0


async def test_max_turns_then_resume():
    async def call(name: str, arguments: dict) -> str:
        return "MAX_RETRIES = 3\n\ndef retry():\n    # tries at most MAX_RETRIES times\n"
    first = await claude_code.run(system="Read the file with the tool before answering.", prompt="What is MAX_RETRIES in retry.py?",
                                  schema=SCHEMA, model="haiku", max_turns=1, toolset=Toolset(DEFS, call), persist=True)
    try:
        assert first.subtype == "error_max_turns"
        second = await claude_code.run(system="Read the file with the tool before answering.", prompt="Answer now from what you read.",
                                       schema=SCHEMA, model="haiku", max_turns=2, toolset=Toolset(DEFS, call), resume=first.session_id,
                                       persist=True, cwd=first.cwd)
        assert second.structured_output == {"retries": 3}
    finally:
        claude_code.forget_session(first)
