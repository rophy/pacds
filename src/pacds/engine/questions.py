"""Render the adapter's answer schema as the questions the agent must investigate."""

from __future__ import annotations

import json
from typing import Any

# The adapter's placeholder for an option without a description.
NO_DESCRIPTION = "No additional instructions."
QUESTION_MARKER = "\nQuestion: "


def describe_questions(schema: dict[str, Any]) -> str:
    try:
        described: Any = _questions(schema)
    except (KeyError, TypeError, ValueError):
        described = schema
    # Question text comes from the client: escape so it cannot open or close prompt blocks.
    payload = json.dumps(described, indent=1, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e")
    return f"<questions>\n{payload}\n</questions>"


def _questions(schema: dict[str, Any]) -> list[dict[str, Any]]:
    answers = _resolve(schema, schema["properties"]["answers"])
    questions = []
    for key, prop in answers["properties"].items():
        prop = _resolve(schema, prop)
        _, marker, text = prop["description"].rpartition(QUESTION_MARKER)
        if not marker:
            raise ValueError(f"no question text for {key}")
        if prop["type"] == "number":
            questions.append({"id": key, "question": text, "answer": "probability of yes"})
        else:
            options = {name: (None if option.get("description") == NO_DESCRIPTION else option.get("description")) for name, option in prop["properties"].items()}
            questions.append({"id": key, "question": text, "options": options})
    return questions


def _resolve(schema: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    reference = node.get("$ref")
    if reference is None:
        return node
    return schema["$defs"][reference.removeprefix("#/$defs/")]
