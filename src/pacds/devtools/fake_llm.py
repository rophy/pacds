"""A scripted OpenAI-compatible chat endpoint for tests and the dev cluster. Never use in production.

With tools offered it lists the repository root once, then declares itself ready.
Without tools, or with tool_choice "none", it fills the requested JSON schema with uniform probabilities.
"""

from __future__ import annotations

import json
import os
from typing import Any

import uvicorn
from fastapi import FastAPI, Request


def _resolve(root: dict[str, Any], reference: str) -> dict[str, Any]:
    node: Any = root
    for part in reference.removeprefix("#/").split("/"):
        node = node[part]
    return node


def fill(schema: dict[str, Any], root: dict[str, Any] | None = None) -> Any:
    root = root or schema
    if "$ref" in schema:
        return fill(_resolve(root, schema["$ref"]), root)
    kind = schema.get("type")
    if kind == "object":
        properties = schema.get("properties", {})
        if properties and all(prop.get("type") == "number" for prop in properties.values()):
            return {name: 1 / len(properties) for name in properties}
        return {name: fill(prop, root) for name, prop in properties.items()}
    if kind in ("number", "integer"):
        return 0.5
    if kind == "boolean":
        return True
    if kind == "array":
        return []
    if kind == "string":
        return (schema.get("enum") or [""])[0]
    return None


def _tool_call(name: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}],
    }


def create_app() -> FastAPI:
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> dict[str, Any]:
        body = await request.json()
        messages = body.get("messages", [])
        if body.get("tools") and body.get("tool_choice") != "none":
            if any(message.get("role") == "tool" for message in messages):
                message = _tool_call("ready_to_answer", {}, "call_ready")
            else:
                message = _tool_call("list_files", {"path": "."}, "call_list")
        else:
            schema = (body.get("response_format") or {}).get("json_schema", {}).get("schema", {"type": "object"})
            message = {"role": "assistant", "content": json.dumps(fill(schema))}
        return {
            "id": "fake",
            "object": "chat.completion",
            "created": 0,
            "model": body.get("model", "fake"),
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if "tool_calls" in message else "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

    return app


def main() -> None:
    uvicorn.run(create_app(), host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8000")))
