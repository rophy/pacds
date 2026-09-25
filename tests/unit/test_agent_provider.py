import asyncio
import json

import httpx
import openai
import pytest
from system_one_adapter.providers.base import Message
from typesafe_sdk import TypeSafeAPIError, TypeSafeError

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


def provider(llm: ScriptedLLM, tools, *, max_turns=5, budget=10.0, handler=None, api="chat_completions") -> AgentProvider:
    client = openai.AsyncOpenAI(
        base_url="http://llm.test/v1",
        api_key="k",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler or llm.handler)),
    )
    return AgentProvider(model_name="m", client=client, tools=tools, max_turns=max_turns, time_budget_seconds=budget, api=api)


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
    llm = ScriptedLLM([tool_call("list_files", {}), tool_call("ready_to_answer", {}, "call_2"), completion({"role": "assistant", "content": "bad"}), ANSWER])
    agent = provider(llm, tools)
    first = await agent.request(MESSAGES, schema=SCHEMA, structured=True)
    retry = [*MESSAGES, Message(role="assistant", content=first.text), Message(role="user", content="fix it")]
    second = await agent.request(retry, schema=SCHEMA, structured=True)
    assert second.text == '{"answers": {}}'
    assert (second.input_tokens, second.output_tokens) == (10, 5)
    assert len(llm.requests) == 4
    assert llm.requests[-1]["messages"][-1] == {"role": "user", "content": "fix it"}


async def test_invalid_tool_arguments_reported_to_model(tools):
    llm = ScriptedLLM([tool_call("read_file", "{not json"), tool_call("ready_to_answer", {}, "call_2"), ANSWER])
    await provider(llm, tools).request(MESSAGES, schema=SCHEMA, structured=True)
    assert llm.requests[1]["messages"][-1]["content"].startswith("error:")


async def test_llm_overload_maps_to_typesafe_error(tools):
    with pytest.raises(TypeSafeAPIError) as error:
        await provider(ScriptedLLM([529]), tools).request(MESSAGES, schema=SCHEMA, structured=True)
    assert error.value.status == 529


def responses_output(*items: dict, status: str = "completed", incomplete: str | None = None) -> dict:
    return {
        "id": "resp", "object": "response", "created_at": 0, "model": "m", "status": status,
        "incomplete_details": {"reason": incomplete} if incomplete else None,
        "output": list(items), "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                  "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}},
    }


def responses_call(name: str, arguments: dict, call_id: str = "call_1") -> dict:
    return responses_output({"type": "function_call", "id": f"fc_{call_id}", "call_id": call_id, "name": name, "arguments": json.dumps(arguments), "status": "completed"})


def responses_text(text: str, **kwargs) -> dict:
    return responses_output({"type": "message", "id": "msg", "role": "assistant", "status": "completed",
                             "content": [{"type": "output_text", "text": text, "annotations": []}]}, **kwargs)


class RecordingLLM(ScriptedLLM):
    def __init__(self, responses):
        super().__init__(responses)
        self.paths: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        return super().handler(request)


async def test_responses_api_investigates_then_answers_with_schema(tools):
    llm = RecordingLLM([responses_call("list_files", {}), responses_call("ready_to_answer", {}, "call_2"), responses_text('{"answers": {}}')])
    result = await provider(llm, tools, api="responses").request(MESSAGES, schema=SCHEMA, structured=True)
    assert result.text == '{"answers": {}}'
    assert (result.input_tokens, result.output_tokens) == (30, 15)
    assert set(llm.paths) == {"/v1/responses"}
    first, second, final = llm.requests
    assert first["input"][0]["role"] == "system" and "untrusted" in first["input"][0]["content"]
    assert {tool["name"] for tool in first["tools"]} >= {"search_code", "ready_to_answer"}
    assert second["input"][-2:] == [
        {"type": "function_call", "call_id": "call_1", "name": "list_files", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_1", "output": "app.py"},
    ]
    assert "tools" not in final
    assert final["text"]["format"]["schema"] == SCHEMA
    assert all(request["store"] is False for request in llm.requests)


async def test_responses_api_incomplete_answer_is_an_error(tools):
    llm = RecordingLLM([responses_call("list_files", {}), responses_call("ready_to_answer", {}, "call_2"), responses_text('{"answ', status="incomplete", incomplete="max_output_tokens")])
    with pytest.raises(TypeSafeError, match="length"):
        await provider(llm, tools, api="responses").request(MESSAGES, schema=SCHEMA, structured=True)


async def test_responses_api_overload_maps_to_typesafe_error(tools):
    with pytest.raises(TypeSafeAPIError) as error:
        await provider(RecordingLLM([429]), tools, api="responses").request(MESSAGES, schema=SCHEMA, structured=True)
    assert error.value.status == 429


def contents(request: dict) -> list[str]:
    return [message.get("content") or "" for message in request["messages"]]


async def test_adapter_system_prompt_is_replaced(tools):
    llm = ScriptedLLM([tool_call("list_files", {}), tool_call("ready_to_answer", {}, "call_2"), ANSWER])
    await provider(llm, tools).request(MESSAGES, schema=SCHEMA, structured=True)
    for request in llm.requests:
        assert "adapter system" not in contents(request)
        assert [message["role"] for message in request["messages"]].count("system") == 1


async def test_questions_come_before_the_document(tools):
    llm = ScriptedLLM([tool_call("list_files", {}), tool_call("ready_to_answer", {}, "call_2"), ANSWER])
    await provider(llm, tools).request(MESSAGES, schema=SCHEMA, structured=True)
    first = contents(llm.requests[0])
    assert first[1].startswith("<questions>") and first[2].startswith("<document>")


async def test_final_instruction_answers_from_the_investigation(tools):
    llm = ScriptedLLM([tool_call("list_files", {}), tool_call("ready_to_answer", {}, "call_2"), ANSWER])
    await provider(llm, tools).request(MESSAGES, schema=SCHEMA, structured=True)
    instruction = contents(llm.requests[-1])[-1]
    assert "investigation" in instruction and "sum to 1" in instruction
    assert "Return one JSON object that matches this schema" not in instruction


async def test_unstructured_final_instruction_carries_the_schema(tools):
    llm = ScriptedLLM([tool_call("list_files", {}), tool_call("ready_to_answer", {}, "call_2"), ANSWER])
    await provider(llm, tools).request(MESSAGES, schema=SCHEMA, structured=False)
    assert json.dumps(SCHEMA) in contents(llm.requests[-1])[-1]


async def test_ready_before_investigating_is_pushed_back_once(tools):
    llm = ScriptedLLM([tool_call("ready_to_answer", {}), tool_call("list_files", {}, "call_2"), tool_call("ready_to_answer", {}, "call_3"), ANSWER])
    await provider(llm, tools).request(MESSAGES, schema=SCHEMA, structured=True)
    pushback = llm.requests[1]["messages"][-1]
    assert pushback["role"] == "tool" and pushback["content"].startswith("error:")
    assert len(llm.requests) == 4


async def test_insisting_on_ready_is_accepted(tools):
    llm = ScriptedLLM([tool_call("ready_to_answer", {}), tool_call("ready_to_answer", {}, "call_2"), ANSWER])
    result = await provider(llm, tools).request(MESSAGES, schema=SCHEMA, structured=True)
    assert result.text == '{"answers": {}}'
    assert len(llm.requests) == 3
