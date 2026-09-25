import asyncio
import json

import httpx
import openai
import pytest
from system_one_adapter.providers.base import Message
from typesafe_sdk import TypeSafeAPIError

from pacds.engine.agent_provider import AgentBudgetExceeded, AgentProvider
from pacds.engine.tools import WorkspaceTools

SCHEMA = {"type": "object", "properties": {"answers": {"type": "object"}}, "required": ["answers"], "additionalProperties": False}
MESSAGES = [Message(role="system", content="adapter system"), Message(role="user", content="<document>{}</document>")]


def completion(message: dict, finish: str = "stop") -> dict:
    return {
        "id": "c",
        "object": "chat.completion",
        "created": 0,
        "model": "m",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def tool_call(name: str, arguments, call_id: str = "call_1") -> dict:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return completion(
        {"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": raw}}]},
        finish="tool_calls",
    )


ANSWER = completion({"role": "assistant", "content": '{"answers": {}}'})


class ScriptedLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(response, int):
            return httpx.Response(response, json={"error": {"message": "overloaded"}})
        return httpx.Response(200, json=response)


@pytest.fixture
def tools(tmp_path) -> WorkspaceTools:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("print('hi')\n")
    logs = tmp_path / "logs"
    logs.mkdir()
    return WorkspaceTools(repo, logs)


def provider(llm: ScriptedLLM, tools, *, max_turns=5, budget=10.0, handler=None) -> AgentProvider:
    client = openai.AsyncOpenAI(
        base_url="http://llm.test/v1",
        api_key="k",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler or llm.handler)),
    )
    return AgentProvider(model_name="m", client=client, tools=tools, max_turns=max_turns, time_budget_seconds=budget)


async def test_investigates_then_answers_with_schema(tools):
    llm = ScriptedLLM([tool_call("list_files", {}), tool_call("ready_to_answer", {}, "call_2"), ANSWER])
    result = await provider(llm, tools).request(MESSAGES, schema=SCHEMA, structured=True)
    assert result.text == '{"answers": {}}'
    assert (result.input_tokens, result.output_tokens) == (30, 15)
    first, second, final = llm.requests
    assert first["messages"][0]["role"] == "system" and "untrusted" in first["messages"][0]["content"]
    assert {tool["function"]["name"] for tool in first["tools"]} >= {"search_code", "ready_to_answer"}
    tool_message = second["messages"][-1]
    assert tool_message == {"role": "tool", "tool_call_id": "call_1", "content": "app.py"}
    assert "tools" not in final
    assert final["response_format"]["json_schema"]["schema"] == SCHEMA


async def test_model_stopping_without_tools_ends_investigation(tools):
    llm = ScriptedLLM([completion({"role": "assistant", "content": "I know enough."}), ANSWER])
    result = await provider(llm, tools).request(MESSAGES, schema=SCHEMA, structured=True)
    assert result.text == '{"answers": {}}'
    assert len(llm.requests) == 2


async def test_turn_budget(tools):
    llm = ScriptedLLM([tool_call("list_files", {})])
    with pytest.raises(AgentBudgetExceeded):
        await provider(llm, tools, max_turns=3).request(MESSAGES, schema=SCHEMA, structured=True)
    assert len(llm.requests) == 3


async def test_time_budget(tools):
    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json=ANSWER)

    with pytest.raises(AgentBudgetExceeded):
        await provider(ScriptedLLM([ANSWER]), tools, budget=0.05, handler=slow).request(MESSAGES, schema=SCHEMA, structured=True)


async def test_corrective_retry_reuses_investigation(tools):
    llm = ScriptedLLM([tool_call("ready_to_answer", {}), completion({"role": "assistant", "content": "bad"}), ANSWER])
    agent = provider(llm, tools)
    first = await agent.request(MESSAGES, schema=SCHEMA, structured=True)
    retry = [*MESSAGES, Message(role="assistant", content=first.text), Message(role="user", content="fix it")]
    second = await agent.request(retry, schema=SCHEMA, structured=True)
    assert second.text == '{"answers": {}}'
    assert (second.input_tokens, second.output_tokens) == (10, 5)
    assert len(llm.requests) == 3
    assert llm.requests[-1]["messages"][-1] == {"role": "user", "content": "fix it"}


async def test_invalid_tool_arguments_reported_to_model(tools):
    llm = ScriptedLLM([tool_call("read_file", "{not json"), tool_call("ready_to_answer", {}, "call_2"), ANSWER])
    await provider(llm, tools).request(MESSAGES, schema=SCHEMA, structured=True)
    assert llm.requests[1]["messages"][-1]["content"].startswith("error:")


async def test_llm_overload_maps_to_typesafe_error(tools):
    with pytest.raises(TypeSafeAPIError) as error:
        await provider(ScriptedLLM([529]), tools).request(MESSAGES, schema=SCHEMA, structured=True)
    assert error.value.status == 529
