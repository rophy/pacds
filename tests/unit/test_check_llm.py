import json

import httpx
import openai

from pacds.devtools.check_llm import check_chat, check_context, check_json_schema, check_tools
from pacds.engine.agent_provider import AgentProvider


def completion(message, finish="stop", prompt_tokens=10):
    return {"id": "c", "object": "chat.completion", "created": 0, "model": "served", "choices": [{"index": 0, "finish_reason": finish, "message": message}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 1, "total_tokens": prompt_tokens + 1}}


def provider(responses):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=responses.pop(0))

    client = openai.AsyncOpenAI(base_url="http://vllm.test/v1", api_key="k", max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return AgentProvider(model_name="served", client=client, tools=None, max_turns=1, time_budget_seconds=10), requests  # type: ignore[arg-type]


async def test_tool_round_trip_passes():
    call = {"id": "t1", "type": "function", "function": {"name": "lookup", "arguments": '{"key": "sky"}'}}
    llm, requests = provider([completion({"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls"),
                              completion({"role": "assistant", "content": "The sky is blue."})])
    assert "used the result" in await check_tools(llm)
    assert requests[1]["messages"][-1] == {"role": "tool", "tool_call_id": "t1", "content": "blue"}


async def test_a_server_without_tool_parsing_fails_the_tools_check():
    import pytest

    llm, _ = provider([completion({"role": "assistant", "content": '<tool_call>{"name": "lookup"}</tool_call>'})])
    with pytest.raises(AssertionError, match="no tool call"):
        await check_tools(llm)


async def test_an_unenforced_schema_fails_the_json_check():
    import pytest

    llm, requests = provider([completion({"role": "assistant", "content": '{"color": "blue"}'})])
    with pytest.raises(AssertionError, match="does not match the schema"):
        await check_json_schema(llm)
    assert requests[0]["response_format"]["type"] == "json_schema"


async def test_context_and_chat():
    llm, requests = provider([completion({"role": "assistant", "content": "ready"}), completion({"role": "assistant", "content": "PELICAN"}, prompt_tokens=64000)])
    assert "ready" in await check_chat(llm)
    assert "64,000 tokens" in await check_context(64000)(llm)
    assert requests[1]["messages"][0]["content"].startswith("The code word is PELICAN.")


async def test_the_json_schema_check_sends_the_tools_like_the_final_request():
    answer = completion({"role": "assistant", "content": '{"color": "blue", "confidence": 0.9}'})
    llm, requests = provider([answer, answer])
    await check_json_schema(llm)
    assert requests[0]["tool_choice"] == "none" and requests[0]["tools"][0]["function"]["name"] == "lookup"
    llm._final_keeps_tools = False
    await check_json_schema(llm)
    assert "tools" not in requests[1]
