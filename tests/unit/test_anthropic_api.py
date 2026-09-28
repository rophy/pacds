import json

import anthropic
import httpx2 as httpx
import pytest
from system_one_adapter.providers.base import Message

from pacds.engine.agent_provider import AgentProvider
from pacds.engine.anthropic_api import RAW_CONTENT, request_kwargs
from pacds.engine.tools import WorkspaceTools
from pacds.engine.trace import Trace

SCHEMA = {"type": "object", "properties": {"answers": {"type": "object"}}, "required": ["answers"], "additionalProperties": False}
MESSAGES = [Message(role="system", content="adapter system"), Message(role="user", content="<document>{}</document>")]
THINKING = {"type": "thinking", "thinking": "", "signature": "sig-1"}


def reply(content, stop="end_turn", cached=0, written=0):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": content,
            "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": cached, "cache_creation_input_tokens": written}}


def tool_use(name, arguments, call_id="toolu_1"):
    return reply([THINKING, {"type": "tool_use", "id": call_id, "name": name, "input": arguments}], stop="tool_use")


class ScriptedMessages:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    def client(self) -> anthropic.AsyncAnthropic:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(json.loads(request.content))
            return httpx.Response(200, json=self.responses.pop(0))

        return anthropic.AsyncAnthropic(base_url="http://llm.test", api_key="k", max_retries=0,
                                        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx.MockTransport(handler)))


@pytest.fixture
def tools(tmp_path) -> WorkspaceTools:
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "app.py").write_text("print('hi')\n")
    (tmp_path / "logs").mkdir()
    return WorkspaceTools(tmp_path / "repo", tmp_path / "logs")


def test_requests_cache_the_prefix_and_group_tool_results():
    messages = [
        {"role": "system", "content": "be careful"},
        {"role": "user", "content": "questions"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "x"}'}},
            {"id": "b", "type": "function", "function": {"name": "list_files", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "content": "x"},
        {"role": "tool", "tool_call_id": "b", "content": ""},
    ]
    tools = [{"type": "function", "function": {"name": "read_file", "description": "Read", "parameters": {"type": "object"}}}]
    kwargs = request_kwargs(messages=messages, tools=tools, effort="high")
    assert kwargs["system"] == "be careful" and kwargs["cache_control"] == {"type": "ephemeral"}
    assert kwargs["output_config"] == {"effort": "high"} and kwargs["tools"][0]["input_schema"] == {"type": "object"}
    user, assistant, results = kwargs["messages"]
    assert [b["type"] for b in assistant["content"]] == ["tool_use", "tool_use"] and assistant["content"][0]["input"] == {"path": "x"}
    assert [b["tool_use_id"] for b in results["content"]] == ["a", "b"] and results["content"][1]["content"] == "(empty)"


def test_the_answer_schema_is_an_instruction():
    kwargs = request_kwargs(messages=[{"role": "user", "content": "q"}],
                            response_format={"type": "json_schema", "json_schema": {"name": "e", "schema": SCHEMA, "strict": True}})
    assert "output_config" not in kwargs and '"answers"' in kwargs["messages"][-1]["content"]


async def test_investigation_replays_thinking_blocks_and_counts_cache(tools):
    llm = ScriptedMessages([
        tool_use("read_file", {"path": "app.py"}),
        tool_use("ready_to_answer", {}, "toolu_2"),
        reply([THINKING, {"type": "text", "text": '```json\n{"answers": {}}\n```'}], cached=80, written=10),
    ])
    trace = Trace()
    provider = AgentProvider(model_name="claude-opus-5-5", client=None, tools=tools, max_turns=5, time_budget_seconds=10,  # type: ignore[arg-type]
                             api="anthropic", anthropic_client=llm.client(), effort="medium", trace=trace)
    result = await provider.request(MESSAGES, schema=SCHEMA, structured=True)
    assert result.text == '{"answers": {}}'
    second = llm.requests[1]
    assert second["cache_control"] == {"type": "ephemeral"} and second["output_config"] == {"effort": "medium"}
    assistant = second["messages"][2]
    assert assistant["role"] == "assistant" and assistant["content"][0] == THINKING
    assert second["messages"][3]["content"][0]["type"] == "tool_result" and "print('hi')" in second["messages"][3]["content"][0]["content"]
    final = trace.calls[-1]
    assert final["usage"]["input"] == 190 and final["usage"]["cached"] == 80 and final["usage"]["cache_write"] == 10
    assert final["response"][RAW_CONTENT][0]["signature"] == "sig-1"
    assert (result.input_tokens, result.output_tokens) == (100 + 100 + 190, 60)


async def test_anthropic_errors_map_to_typesafe_errors(tools):
    from typesafe_sdk import TypeSafeAPIError

    def overloaded(request):
        return httpx.Response(529, json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}})

    client = anthropic.AsyncAnthropic(base_url="http://llm.test", api_key="k", max_retries=0,
                                      http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx.MockTransport(overloaded)))
    provider = AgentProvider(model_name="m", client=None, tools=tools, max_turns=5, time_budget_seconds=10, api="anthropic", anthropic_client=client)  # type: ignore[arg-type]
    with pytest.raises(TypeSafeAPIError) as error:
        await provider.request(MESSAGES, schema=SCHEMA, structured=True)
    assert error.value.status == 529
