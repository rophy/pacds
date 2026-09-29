"""Translate between the chat-completions shape the agent loop uses and Anthropic's Messages API.

Thinking cannot be turned off on current Claude models, and their thinking blocks must be sent back unchanged in the
tool loop: each response's content blocks ride along on the assistant message under RAW_CONTENT and are replayed as
they came. Every request caches its whole prefix (top-level cache_control), so each turn of an investigation reads
the turns before it from the cache instead of paying for them again.
"""

from __future__ import annotations

import json
from typing import Any

from openai.types.chat import ChatCompletion

# Key on an assistant message holding the response's own content blocks (thinking, text, tool_use).
RAW_CONTENT = "anthropic_content"
DEFAULT_MAX_TOKENS = 16000
STOP_REASONS = {"end_turn": "stop", "stop_sequence": "stop", "tool_use": "tool_calls", "max_tokens": "length",
                "refusal": "content_filter", "pause_turn": "length"}
SCHEMA_INSTRUCTION = "Return one JSON object that matches this schema exactly:\n\n{schema}\n\nDo not add text or Markdown fencing around it."


def request_kwargs(
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    response_format: dict[str, Any] | None = None,
    max_tokens: int | None = None,
    effort: str | None = None,
    tool_choice: str | None = None,
) -> dict[str, Any]:
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    turns = _messages([m for m in messages if m["role"] != "system"])
    if response_format is not None and response_format["type"] == "json_schema":
        # The schema as an instruction rather than output_config.format: the answer schemas use JSON Schema
        # keywords that structured outputs does not accept; the adapter validates and retries malformed answers.
        schema = json.dumps(response_format["json_schema"]["schema"])
        turns.append({"role": "user", "content": SCHEMA_INSTRUCTION.format(schema=schema)})
    kwargs: dict[str, Any] = {"max_tokens": max_tokens or DEFAULT_MAX_TOKENS, "messages": turns, "cache_control": {"type": "ephemeral"}}
    if system:
        kwargs["system"] = system
    if tools:
        kwargs["tools"] = [_tool(tool) for tool in tools]
    if tool_choice is not None:
        kwargs["tool_choice"] = {"type": tool_choice}
    if effort:
        kwargs["output_config"] = {"effort": effort}
    return kwargs


def _messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for message in messages:
        if message["role"] == "tool":
            result = {"type": "tool_result", "tool_use_id": message["tool_call_id"], "content": message["content"] or "(empty)"}
            # Every result of one assistant turn goes back in a single user message.
            if turns and turns[-1]["role"] == "user" and isinstance(turns[-1]["content"], list) and \
                    all(block.get("type") == "tool_result" for block in turns[-1]["content"]):
                turns[-1]["content"].append(result)
            else:
                turns.append({"role": "user", "content": [result]})
        elif message["role"] == "assistant":
            turns.append({"role": "assistant", "content": message.get(RAW_CONTENT) or _assistant_blocks(message)})
        else:
            turns.append({"role": message["role"], "content": message.get("content") or ""})
    return turns


def _assistant_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [{"type": "text", "text": message["content"]}] if message.get("content") else []
    for call in message.get("tool_calls") or []:
        try:
            arguments = json.loads(call["function"]["arguments"] or "{}")
        except json.JSONDecodeError:
            arguments = {}
        blocks.append({"type": "tool_use", "id": call["id"], "name": call["function"]["name"],
                       "input": arguments if isinstance(arguments, dict) else {}})
    return blocks


def _tool(tool: dict[str, Any]) -> dict[str, Any]:
    function = tool["function"]
    return {"name": function["name"], "description": function.get("description") or "", "input_schema": function["parameters"]}


def _unfenced(text: str) -> str:
    """A final JSON answer sometimes comes wrapped in a Markdown code fence despite the instruction."""
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        return stripped.split("\n", 1)[1].rsplit("```", 1)[0].strip() if "\n" in stripped else stripped
    return text


def from_message(message: Any) -> ChatCompletion:
    text = _unfenced("".join(block.text for block in message.content if block.type == "text"))
    calls = [
        {"id": block.id, "type": "function", "function": {"name": block.name, "arguments": json.dumps(block.input)}}
        for block in message.content
        if block.type == "tool_use"
    ]
    usage = message.usage
    cached = usage.cache_read_input_tokens or 0
    written = usage.cache_creation_input_tokens or 0
    prompt = (usage.input_tokens or 0) + cached + written
    return ChatCompletion.model_validate({
        "id": message.id,
        "object": "chat.completion",
        "created": 0,
        "model": message.model,
        "choices": [{"index": 0, "finish_reason": STOP_REASONS.get(message.stop_reason or "", "stop"), "message": {
            "role": "assistant", "content": text or None, "tool_calls": calls or None,
            RAW_CONTENT: [block.model_dump(mode="json", exclude_none=True) for block in message.content],
        }}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": usage.output_tokens or 0, "total_tokens": prompt + (usage.output_tokens or 0),
                  "prompt_tokens_details": {"cached_tokens": cached}, "cache_creation_input_tokens": written},
    })
