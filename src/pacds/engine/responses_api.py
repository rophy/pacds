"""Translate between the chat-completions shape the agent loop uses and OpenAI's Responses API."""

from __future__ import annotations

from typing import Any

from openai.types.chat import ChatCompletion
from openai.types.responses import Response

# Responses reports why generation stopped early; map to the chat-completions finish reasons.
INCOMPLETE_REASONS = {"max_output_tokens": "length", "content_filter": "content_filter"}


def request_kwargs(
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    response_format: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Stateless like chat completions: every request carries the whole transcript.
    kwargs: dict[str, Any] = {"input": _input_items(messages), "store": False}
    if tools is not None:
        kwargs["tools"] = [_tool(tool) for tool in tools]
    if response_format is not None:
        kwargs["text"] = {"format": _text_format(response_format)}
    return kwargs


def _input_items(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        if message["role"] == "tool":
            items.append({"type": "function_call_output", "call_id": message["tool_call_id"], "output": message["content"]})
            continue
        calls = message.get("tool_calls") or []
        if message.get("content") or not calls:
            items.append({"role": message["role"], "content": message.get("content") or ""})
        for call in calls:
            function = call["function"]
            items.append({"type": "function_call", "call_id": call["id"], "name": function["name"], "arguments": function["arguments"]})
    return items


def _tool(tool: dict[str, Any]) -> dict[str, Any]:
    function = tool["function"]
    # Chat-completions tools are non-strict unless they say otherwise; keep that on /responses.
    return {"type": "function", "name": function["name"], "description": function.get("description"), "parameters": function["parameters"], "strict": False}


def _text_format(response_format: dict[str, Any]) -> dict[str, Any]:
    if response_format["type"] == "json_schema":
        return {"type": "json_schema", **response_format["json_schema"]}
    return {"type": response_format["type"]}


def from_response(response: Response) -> ChatCompletion:
    text = "".join(part.text for item in response.output if item.type == "message" for part in item.content if part.type == "output_text")
    calls = [
        {"id": item.call_id, "type": "function", "function": {"name": item.name, "arguments": item.arguments}}
        for item in response.output
        if item.type == "function_call"
    ]
    if response.status == "incomplete":
        reason = response.incomplete_details.reason if response.incomplete_details else None
        finish = INCOMPLETE_REASONS.get(reason or "", "length")
    else:
        finish = "tool_calls" if calls else "stop"
    usage = response.usage
    return ChatCompletion.model_validate({
        "id": response.id,
        "object": "chat.completion",
        "created": int(response.created_at),
        "model": response.model,
        "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": text or None, "tool_calls": calls or None}}],
        "usage": {"prompt_tokens": usage.input_tokens, "completion_tokens": usage.output_tokens, "total_tokens": usage.total_tokens} if usage else None,
    })
