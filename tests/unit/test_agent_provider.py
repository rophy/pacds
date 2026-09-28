import asyncio
import json

import httpx
import openai
import pytest
from system_one_adapter.providers.base import Message
from typesafe_sdk import TypeSafeAPIError, TypeSafeError

from pacds.engine.agent_provider import AgentBudgetExceeded, AgentProvider
from pacds.engine.tools import WorkspaceTools
from pacds.engine.trace import Trace, messages_at

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


def provider(llm: ScriptedLLM, tools, *, max_turns=5, budget=10.0, handler=None, api="chat_completions", max_output_tokens=None,
             trace=None) -> AgentProvider:
    client = openai.AsyncOpenAI(
        base_url="http://llm.test/v1",
        api_key="k",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler or llm.handler)),
    )
    return AgentProvider(model_name="m", client=client, tools=tools, max_turns=max_turns, time_budget_seconds=budget, api=api,
                         max_output_tokens=max_output_tokens, trace=trace)


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



async def test_tool_calls_are_logged_with_the_request_id(tools, caplog):
    import logging

    from pacds.context import request_id

    llm = ScriptedLLM([tool_call("read_file", {"path": "app.py"}), tool_call("ready_to_answer", {}, "call_2"), ANSWER])
    token = request_id.set("req123")
    try:
        with caplog.at_level(logging.INFO, logger="pacds.engine.agent_provider"):
            await provider(llm, tools).request(MESSAGES, schema=SCHEMA, structured=True)
    finally:
        request_id.reset(token)
    lines = [record.getMessage() for record in caplog.records]
    assert any("request=req123" in line and "tool=read_file" in line and '"path": "app.py"' in line and "result_chars=" in line for line in lines)
    assert not any("print('hi')" in line for line in lines), "tool results (source code) must not be logged"
    assert any("request=req123" in line and "investigation ended" in line and "turns=2" in line for line in lines)



async def test_llm_call_timeout_is_not_budget_exhaustion(tools):
    from typesafe_sdk import TypeSafeAPITimeoutError

    def hang(request):
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(TypeSafeAPITimeoutError):
        await provider(ScriptedLLM([ANSWER]), tools, budget=10.0, handler=hang).request(MESSAGES, schema=SCHEMA, structured=True)


def test_system_prompt_is_general_not_incident_specific():
    from pacds.engine.agent_provider import AGENT_SYSTEM_PROMPT

    prompt = AGENT_SYSTEM_PROMPT.lower()
    # How to investigate: valid for any question about the application.
    for phrase in ("deliberate", "originates", "cannot produce", "regression", "untrusted"):
        assert phrase in prompt, phrase
    # What is being asked belongs to the client's questions, not to PACDS.
    for phrase in ("incident", "support team", "works as designed", "defect", "proxy", "browser"):
        assert phrase not in prompt, phrase



async def test_output_token_limit_is_sent_on_every_call_when_set(tools):
    llm = ScriptedLLM([tool_call("ready_to_answer", {}), ANSWER])
    await provider(llm, tools, max_output_tokens=16000).request(MESSAGES, schema=SCHEMA, structured=True)
    assert llm.requests and all(request["max_tokens"] == 16000 for request in llm.requests)
    llm = ScriptedLLM([tool_call("ready_to_answer", {}), ANSWER])
    await provider(llm, tools).request(MESSAGES, schema=SCHEMA, structured=True)
    assert all("max_tokens" not in request for request in llm.requests)


async def test_output_token_limit_uses_the_responses_api_name(tools):
    llm = ScriptedLLM([responses_call("ready_to_answer", {}), responses_output({"type": "message", "role": "assistant",
                       "content": [{"type": "output_text", "text": '{"answers": {}}'}]})])
    await provider(llm, tools, api="responses", max_output_tokens=16000).request(MESSAGES, schema=SCHEMA, structured=True)
    assert all(request["max_output_tokens"] == 16000 and "max_tokens" not in request for request in llm.requests)


async def test_gateway_errors_retry_the_call_not_the_investigation(tools, monkeypatch):
    from pacds.engine import agent_provider

    monkeypatch.setattr(agent_provider, "GATEWAY_RETRY_DELAYS_SECONDS", (0.0, 0.0))
    llm = ScriptedLLM([502, tool_call("ready_to_answer", {}), 504, ANSWER])
    result = await provider(llm, tools).request(MESSAGES, schema=SCHEMA, structured=True)
    assert result.text == '{"answers": {}}'


async def test_persistent_gateway_errors_still_fail(tools, monkeypatch):
    from pacds.engine import agent_provider

    monkeypatch.setattr(agent_provider, "GATEWAY_RETRY_DELAYS_SECONDS", (0.0, 0.0))
    llm = ScriptedLLM([502])
    with pytest.raises(TypeSafeAPIError):
        await provider(llm, tools).request(MESSAGES, schema=SCHEMA, structured=True)
    assert len(llm.requests) == 3


async def test_trace_records_every_call_as_a_delta(tools):
    llm = ScriptedLLM([tool_call("list_files", {}), tool_call("ready_to_answer", {}, "call_2"), completion({"role": "assistant", "content": "bad"}), ANSWER])
    trace = Trace()
    agent = provider(llm, tools, trace=trace)
    first = await agent.request(MESSAGES, schema=SCHEMA, structured=True)
    retry = [*MESSAGES, Message(role="assistant", content=first.text), Message(role="user", content="fix it")]
    second = await agent.request(retry, schema=SCHEMA, structured=True)
    assert [call["phase"] for call in trace.calls] == ["investigate", "investigate", "final", "correction"]
    for n, sent in enumerate(llm.requests, start=1):
        assert messages_at(trace.calls, n) == sent["messages"]
    # Each call adds only what is new: the next turn's messages, not the whole transcript again.
    assert [call["kept"] for call in trace.calls] == [0, 3, 5, 8]
    assert trace.calls[0]["response"]["tool_calls"] == [{"id": "call_1", "name": "list_files", "arguments": "{}"}]
    assert trace.calls[2]["response"]["content"] == "bad" and trace.info["final_raw"] == '{"answers": {}}'
    usage = trace.usage()
    assert (usage["input"], usage["output"], usage["calls"]) == (first.input_tokens + second.input_tokens, first.output_tokens + second.output_tokens, 4)
    assert trace.info["investigation"] == {"turns": 2, "reason": "ready"}


async def test_trace_keeps_tool_results(tools):
    llm = ScriptedLLM([tool_call("read_file", {"path": "app.py"}), tool_call("ready_to_answer", {}, "call_2"), ANSWER])
    trace = Trace()
    await provider(llm, tools, trace=trace).request(MESSAGES, schema=SCHEMA, structured=True)
    [tool] = trace.tools
    assert tool["call"] == 1 and tool["name"] == "read_file" and "print('hi')" in tool["result"]
    assert tool["result_chars"] == len(tool["result"]) and tool["error"] is False


async def test_trace_records_gateway_retries_as_attempts(tools, monkeypatch):
    from pacds.engine import agent_provider

    monkeypatch.setattr(agent_provider, "GATEWAY_RETRY_DELAYS_SECONDS", (0.0, 0.0))
    trace = Trace()
    llm = ScriptedLLM([502, tool_call("ready_to_answer", {}), ANSWER])
    await provider(llm, tools, trace=trace).request(MESSAGES, schema=SCHEMA, structured=True)
    assert [attempt["status"] for attempt in trace.calls[0]["attempts"]] == [502, 200]
    assert len(trace.calls) == 3 and trace.usage()["calls"] == 3


async def test_trace_marks_time_budget_exhaustion(tools):
    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json=ANSWER)

    trace = Trace()
    with pytest.raises(AgentBudgetExceeded):
        await provider(ScriptedLLM([ANSWER]), tools, budget=0.05, handler=slow, trace=trace).request(MESSAGES, schema=SCHEMA, structured=True)
    assert trace.info["investigation"]["reason"] == "time_budget"
    assert trace.calls[0]["response"] is None and trace.calls[0]["attempts"][0]["status"] is None


async def test_unchanged_rerun_replays_every_call(tools):
    from pacds.engine.replay import Recordings

    recorded = Trace()
    original = await provider(ScriptedLLM([tool_call("read_file", {"path": "app.py"}), tool_call("ready_to_answer", {}, "call_2"), ANSWER]),
                              tools, trace=recorded).request(MESSAGES, schema=SCHEMA, structured=True)
    offline = ScriptedLLM([500])  # any live call would fail
    replayed = Trace()
    agent = AgentProvider(model_name="m", client=provider(offline, tools)._client, tools=tools, max_turns=5, time_budget_seconds=10,
                          trace=replayed, replay=Recordings([recorded.to_dict()]))
    result = await agent.request(MESSAGES, schema=SCHEMA, structured=True)
    assert offline.requests == []
    assert (result.text, result.input_tokens, result.output_tokens) == (original.text, original.input_tokens, original.output_tokens)
    assert replayed.usage()["replayed_calls"] == 3 and replayed.usage()["live_input"] == 0
    assert [c["request_sha256"] for c in replayed.calls] == [c["request_sha256"] for c in recorded.calls]


async def test_changed_final_instruction_reruns_only_the_final_call(tools, monkeypatch):
    from pacds.engine import agent_provider
    from pacds.engine.replay import Recordings

    recorded = Trace()
    await provider(ScriptedLLM([tool_call("read_file", {"path": "app.py"}), tool_call("ready_to_answer", {}, "call_2"), ANSWER]),
                   tools, trace=recorded).request(MESSAGES, schema=SCHEMA, structured=True)
    monkeypatch.setattr(agent_provider, "FINAL_INSTRUCTION", "Answer now, differently.")
    live = ScriptedLLM([ANSWER])
    replayed = Trace()
    agent = AgentProvider(model_name="m", client=provider(live, tools)._client, tools=tools, max_turns=5, time_budget_seconds=10,
                          trace=replayed, replay=Recordings([recorded.to_dict()]))
    await agent.request(MESSAGES, schema=SCHEMA, structured=True)
    assert len(live.requests) == 1 and live.requests[0]["messages"][-1]["content"] == "Answer now, differently."
    assert [bool(c.get("replayed")) for c in replayed.calls] == [True, True, False]


async def test_each_recorded_repeat_is_served_once(tools):
    from pacds.engine.replay import Recordings

    recordings = Trace(), Trace()
    for trace, answer in zip(recordings, ('{"answers": {"a": 1}}', '{"answers": {"a": 2}}')):
        llm = ScriptedLLM([tool_call("list_files", {}), tool_call("ready_to_answer", {}, "call_2"), completion({"role": "assistant", "content": answer})])
        await provider(llm, tools, trace=trace).request(MESSAGES, schema=SCHEMA, structured=True)
    store = Recordings([t.to_dict() for t in recordings])
    live = ScriptedLLM([tool_call("list_files", {}), tool_call("ready_to_answer", {}, "call_2"), ANSWER])
    texts = []
    for _ in range(3):
        agent = AgentProvider(model_name="m", client=provider(live, tools)._client, tools=tools, max_turns=5, time_budget_seconds=10, replay=store)
        texts.append((await agent.request(MESSAGES, schema=SCHEMA, structured=True)).text)
    assert texts[:2] == ['{"answers": {"a": 1}}', '{"answers": {"a": 2}}'] and texts[2] == '{"answers": {}}'
    assert len(live.requests) == 3  # the third repeat had no recording left: all live


async def test_a_recorded_failure_is_replayed_so_retries_take_the_same_path(tools):
    from pacds.engine.replay import Recordings

    recorded = Trace()
    trace_call = recorded.start_call("investigate", [{"role": "system", "content": "x"}], {"model": "m", "messages": []})
    trace_call.attempt(0.0, status=None, error="APIConnectionError('Connection error.')")
    store = Recordings([{"calls": [{**recorded.calls[0], "request_sha256": "h"}]}])
    failure = store.take("h")
    with pytest.raises(openai.APIConnectionError):
        failure.raise_()
    assert store.take("h") is None  # served once


async def test_a_call_cancelled_by_the_time_budget_is_not_replayed():
    from pacds.engine.replay import Recordings

    store = Recordings([{"calls": [{"request_sha256": "h", "response": None, "attempts": [{"status": None, "error": "CancelledError()"}]}]}])
    assert store.recorded == 0 and store.take("h") is None
