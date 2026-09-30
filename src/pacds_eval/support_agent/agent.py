"""Support agent for the e2e evaluation: triages a ticket using the tech-support and pacds skills.

The agent is air-gapped: it knows the application's name, the report and the logs, and its only
outside tool is call_pacds. The code version is bound to the ticket's deployment by the harness.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import openai

from pacds.engine import claude_code, responses_api
from pacds.engine.mcp_http import Toolset
from pacds.engine.replay import RecordedFailure, Recordings
from pacds.engine.trace import Trace, sha256
from pacds_eval.harness import Case

SKILLS_DIR = Path(__file__).parents[1] / "skills"
CLASSES = ("A", "B", "C", "D")

# Sends one PACDS request body for a case; returns {"answers": ...} or {"error": {status, code, message}},
# plus the PACDS "request_id" when known (recorded in the outcome, not shown to the agent).
PacdsCaller = Callable[[Case, dict[str, Any]], Awaitable[dict[str, Any]]]
# Returns a URL PACDS can fetch one of the ticket's attachments from, e.g. a freshly presigned one.
# The agent names attachments; URLs never pass through the model, which cannot copy them reliably.
AttachmentUrl = Callable[[str], str]

CALL_PACDS_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "call_pacds",
        "description": "Ask PACDS questions about the application's code and logs. See the pacds skill.",
        "parameters": {
            "type": "object",
            "properties": {
                "document": {"type": "object", "description": "Context for PACDS, e.g. {\"user_report\": \"...\"}"},
                "logs": {"type": "array", "items": {"type": "string"},
                         "description": "Names of the ticket's attached log files to include, e.g. [\"server.log\"]"},
                "questions": {"type": "object", "description": "Questions keyed by id (noul, choice or score)"},
            },
            "required": ["document", "logs", "questions"],
        },
    },
}

SUBMIT_DECISION_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "submit_decision",
        "description": "Submit your triage decision for this ticket. Ends the ticket.",
        "parameters": {
            "type": "object",
            "properties": {
                "class": {"type": "string", "enum": list(CLASSES)},
                "escalate": {"type": "boolean", "description": "Escalate to the development team"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["class", "escalate", "confidence"],
        },
    },
}


@dataclass
class Outcome:
    decision: str | None = None
    escalate: bool | None = None
    confidence: float | None = None
    turns: int = 0
    pacds_requests: list[dict[str, Any]] = field(default_factory=list)
    invalid_requests: int = 0
    pacds_errors: int = 0
    input_tokens: int = 0
    # Every message after the system prompt: the ticket, the agent's turns and the tool results.
    transcript: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    repeat: int = 1


def load_skill(name: str) -> str:
    return (SKILLS_DIR / name / "SKILL.md").read_text()


def application_name(repo: str) -> str:
    base = re.sub(r"\.git$", "", repo.rstrip("/")).rsplit("/", 1)[-1]
    return " ".join(part.capitalize() for part in base.split("-"))


def ticket_message(case: Case, log_texts: dict[str, str]) -> str:
    parts = [f"New ticket.\n\n## User report\n\n{case.report}"]
    for name, text in log_texts.items():
        parts.append(f"## Attached log: {name}\n\n```\n{text}\n```")
    if log_texts:
        parts.append("## Attachments (for tools that take log files)\n\n" + ", ".join(log_texts))
    return "\n\n".join(parts)


def _system_prompt(case: Case, with_pacds: bool) -> str:
    skills = [load_skill("tech-support")] + ([load_skill("pacds")] if with_pacds else [])
    return (f"You handle support tickets for {application_name(case.repo)}, an application your organization operates.\n"
            "Use your skills below. Submit a decision for every ticket.\n\n" + "\n\n---\n\n".join(skills))


async def _complete(client: openai.AsyncOpenAI, model: str, api: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                    max_output_tokens: int | None = None, trace: Trace | None = None, replay: Recordings | None = None,
                    extra_body: dict[str, Any] | None = None) -> Any:
    request = {"model": model, "messages": messages, "tools": tools, "max_output_tokens": max_output_tokens}
    record = trace.start_call("agent", messages, request) if trace is not None else None
    recorded = replay.take(sha256(request)) if replay is not None else None
    if isinstance(recorded, RecordedFailure):
        if record is not None:
            record.replayed()
            record.attempt(time.monotonic(), status=recorded.status, error=recorded.error)
        recorded.raise_()
    if recorded is not None:
        if record is not None:
            record.replayed()
            record.attempt(time.monotonic())
            record.respond(recorded, time.monotonic())
        return recorded
    if record is None:
        return await _send(client, model, api, messages, tools, max_output_tokens, extra_body)
    started = time.monotonic()
    try:
        response = await _send(client, model, api, messages, tools, max_output_tokens, extra_body)
    except BaseException as error:
        record.attempt(started, status=getattr(error, "status_code", None), error=repr(error))
        raise
    record.attempt(started)
    record.respond(response, started)
    return response


async def _send(client: openai.AsyncOpenAI, model: str, api: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                max_output_tokens: int | None, extra_body: dict[str, Any] | None = None) -> Any:
    passthrough = {"extra_body": extra_body} if extra_body else {}
    if api == "responses":
        request = responses_api.request_kwargs(messages=messages, tools=tools)
        if max_output_tokens:
            request["max_output_tokens"] = max_output_tokens
        return responses_api.from_response(await client.responses.create(model=model, **request, **passthrough))
    extra = {"max_tokens": max_output_tokens} if max_output_tokens else {}
    return await client.chat.completions.create(model=model, messages=messages, tools=tools, **extra, **passthrough)


async def run_agent(
    case: Case,
    *,
    log_texts: dict[str, str],
    attachment_url: AttachmentUrl | None = None,
    client: openai.AsyncOpenAI,
    model: str,
    pacds: PacdsCaller | None,
    api: str = "chat_completions",
    max_turns: int = 10,
    max_pacds_calls: int = 3,
    max_output_tokens: int | None = None,
    trace: Trace | None = None,
    replay: Recordings | None = None,
    extra_body: dict[str, Any] | None = None,
    effort: str | None = None,
    call_timeout: float | None = None,
) -> Outcome:
    """call_timeout: the API path's per model call limit (its client timeout); a claude_code session, one CLI run for the whole
    ticket, gets that per turn plus PACDS_TIMEOUT_SECONDS per PACDS call."""
    if api == "claude_code":
        timeout = None if call_timeout is None else call_timeout * (max_turns + 1) + (PACDS_TIMEOUT_SECONDS * max_pacds_calls if pacds else 0)
        return await _run_claude_code(case, log_texts=log_texts, attachment_url=attachment_url, model=model, pacds=pacds, max_turns=max_turns,
                                      max_pacds_calls=max_pacds_calls, trace=trace, replay=replay, effort=effort, timeout=timeout)
    tools = ([CALL_PACDS_TOOL] if pacds else []) + [SUBMIT_DECISION_TOOL]
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _system_prompt(case, with_pacds=pacds is not None)},
        {"role": "user", "content": ticket_message(case, log_texts)},
    ]
    outcome = Outcome()
    try:
        attachments = _Attachments(list(log_texts), attachment_url)
        await _converse(case, messages, tools, outcome, client=client, model=model, pacds=pacds, attachments=attachments, api=api,
                        max_turns=max_turns, max_pacds_calls=max_pacds_calls, max_output_tokens=max_output_tokens, trace=trace, replay=replay,
                        extra_body=extra_body)
    except Exception as error:  # noqa: BLE001 - keep the partial trace of a failed ticket
        outcome.error = repr(error)[:300]
    outcome.transcript = messages[1:]
    return outcome


async def _converse(case: Case, messages: list[dict[str, Any]], tools: list[dict[str, Any]], outcome: Outcome, *, client: openai.AsyncOpenAI,
                    model: str, pacds: PacdsCaller | None, attachments: _Attachments, api: str, max_turns: int,
                    max_pacds_calls: int, max_output_tokens: int | None = None, trace: Trace | None = None,
                    replay: Recordings | None = None, extra_body: dict[str, Any] | None = None) -> None:
    for turn in range(1, max_turns + 1):
        outcome.turns = turn
        response = await _complete(client, model, api, messages, tools, max_output_tokens, trace, replay, extra_body)
        if response.usage is not None:
            outcome.input_tokens += response.usage.prompt_tokens or 0
        message = response.choices[0].message
        calls = message.tool_calls or []
        assistant: dict[str, Any] = {"role": "assistant", "content": message.content or ""}
        if calls:
            assistant["tool_calls"] = [
                {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}} for c in calls
            ]
        messages.append(assistant)
        if not calls:
            messages.append({"role": "user", "content": "Use your tools; finish with submit_decision."})
            continue
        for call in calls:
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = None
            if call.function.name == "submit_decision" and isinstance(arguments, dict) and arguments.get("class") in CLASSES:
                outcome.decision = arguments["class"]
                outcome.escalate = bool(arguments.get("escalate"))
                outcome.confidence = arguments.get("confidence")
                return
            started, requests_before = time.monotonic(), len(outcome.pacds_requests)
            result = await _tool_result(case, call.function.name, arguments, pacds, attachments, outcome, max_pacds_calls)
            if trace is not None:
                # Links the agent's question to PACDS's own trace of the investigation.
                linked = {"pacds_request_id": outcome.pacds_requests[-1].get("request_id")} if len(outcome.pacds_requests) > requests_before else {}
                trace.add_tool(call.function.name, call.function.arguments, result, started, **linked)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})


# The runner's HTTP timeout for one PACDS request (pacds_eval/support_agent/run.py).
PACDS_TIMEOUT_SECONDS = 600

DECISION_NOTE = "Submit your decision as your final structured output (class, escalate, confidence): that is your submit_decision."


async def _run_claude_code(case: Case, *, log_texts: dict[str, str], attachment_url: AttachmentUrl | None, model: str, pacds: PacdsCaller | None,
                           max_turns: int, max_pacds_calls: int, trace: Trace | None, replay: Recordings | None,
                           effort: str | None, timeout: float | None = None) -> Outcome:
    outcome = Outcome()
    prompt = ticket_message(case, log_texts)
    try:
        attachments = _Attachments(list(log_texts), attachment_url)
        await _converse_claude_code(case, prompt, outcome, model=model, pacds=pacds, attachments=attachments, max_turns=max_turns,
                                    max_pacds_calls=max_pacds_calls, trace=trace, replay=replay, effort=effort, timeout=timeout)
    except Exception as error:  # noqa: BLE001 - keep the partial trace of a failed ticket
        outcome.error = repr(error)[:300]
        outcome.transcript = outcome.transcript or [{"role": "user", "content": prompt}]
    return outcome


async def _converse_claude_code(case: Case, prompt: str, outcome: Outcome, *, model: str, pacds: PacdsCaller | None, attachments: _Attachments,
                                max_turns: int, max_pacds_calls: int, trace: Trace | None, replay: Recordings | None,
                                effort: str | None, timeout: float | None = None) -> None:
    system = _system_prompt(case, with_pacds=pacds is not None) + "\n\n" + DECISION_NOTE
    schema = SUBMIT_DECISION_TOOL["function"]["parameters"]
    definitions = [CALL_PACDS_TOOL] if pacds is not None else []
    budget = max_turns + 1  # the StructuredOutput turn
    request = {"api": "claude_code", "model": model, "effort": effort, "system": system, "prompt": prompt, "schema": schema, "tools": definitions,
               "max_turns": budget}
    digest = sha256(request)
    # A ticket that consults PACDS never replays: PACDS's answers are live, so the session's inputs are not the request (spec section 4).
    recorded = replay.take_session(digest) if replay is not None and pacds is None else None
    if recorded is not None:
        result = recorded
        if trace is not None:
            trace.add_session(result, request_sha256=digest, label="agent", replayed=True, tools_from_result=True)
    else:
        async def call(name: str, arguments: dict[str, Any]) -> str:
            started, requests_before = time.monotonic(), len(outcome.pacds_requests)
            text = await _tool_result(case, name, arguments, pacds, attachments, outcome, max_pacds_calls)
            if trace is not None:
                linked = {"pacds_request_id": outcome.pacds_requests[-1].get("request_id")} if len(outcome.pacds_requests) > requests_before else {}
                trace.add_tool(name, json.dumps(arguments), text, started, **linked)
            return text

        toolset = Toolset(definitions, call) if definitions else None
        tools_from = len(trace.tools) if trace is not None else None
        result = await claude_code.run(system=system, prompt=prompt, schema=schema, model=model, max_turns=budget, toolset=toolset, effort=effort,
                                       timeout=timeout)
        if trace is not None:
            trace.add_session(result, request_sha256=digest, label="agent", live_tools_from=tools_from)
    outcome.turns = result.num_turns
    outcome.input_tokens = result.usage.get("input", 0)
    outcome.transcript = [{"role": "user", "content": prompt}]
    for turn in result.turns:
        message: dict[str, Any] = {"role": "assistant", "content": turn.text}
        if turn.tool_calls:
            message["tool_calls"] = [{"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
                                     for c in turn.tool_calls]
        outcome.transcript.append(message)
        for c in turn.tool_calls:
            outcome.transcript.append({"role": "tool", "tool_call_id": c["id"], "content": result.tool_results.get(c["id"], "")})
    answer = result.structured_output
    if isinstance(answer, dict) and answer.get("class") in CLASSES:
        outcome.decision = answer["class"]
        outcome.escalate = bool(answer.get("escalate"))
        outcome.confidence = answer.get("confidence")
    elif result.subtype == "error_max_turns":
        outcome.error = "turn budget exhausted"
    else:
        outcome.error = "no valid decision"


class _Attachments:
    """The ticket's log attachments: the agent names them, PACDS gets a URL made at call time."""

    def __init__(self, names: list[str], url: AttachmentUrl | None) -> None:
        self.names, self._url = names, url

    def resolve(self, requested: Any) -> list[dict[str, str]] | str:
        requested = requested or []
        if not isinstance(requested, list) or not all(isinstance(name, str) for name in requested):
            return 'error: logs must be a list of attachment names, e.g. ["server.log"]'
        unknown = [name for name in requested if name not in self.names]
        if unknown:
            return f"error: no attachment named {', '.join(unknown)}; this ticket has: {', '.join(self.names) or 'none'}"
        if requested and self._url is None:
            return "error: attachments are not available to tools for this ticket"
        return [{"name": name, "url": self._url(name)} for name in requested]  # type: ignore[misc]


async def _tool_result(case: Case, name: str, arguments: Any, pacds: PacdsCaller | None, attachments: _Attachments,
                       outcome: Outcome, max_calls: int) -> str:
    if not isinstance(arguments, dict):
        return "error: arguments must be a JSON object"
    if name == "submit_decision":
        return "error: class must be one of A, B, C, D"
    if name != "call_pacds" or pacds is None:
        return f"error: unknown tool {name}"
    if len(outcome.pacds_requests) >= max_calls:
        return f"error: call_pacds limit of {max_calls} calls reached for this ticket; decide with what you have"
    logs = attachments.resolve(arguments.get("logs"))
    if isinstance(logs, str):
        return logs
    document = arguments.get("document") if isinstance(arguments.get("document"), dict) else {}
    body = {
        "model": "pacds",
        "state": {**document, "pacds": {"git": {"url": case.repo, "ref": case.ref}, "logs": logs}},
        "questions": arguments.get("questions"),
    }
    record: dict[str, Any] = {"questions": arguments.get("questions"), "logs": [log["name"] for log in logs]}
    outcome.pacds_requests.append(record)
    reply = dict(await pacds(case, body))
    record["request_id"] = reply.pop("request_id", None)
    record["reply"] = reply
    if "error" in reply:
        outcome.pacds_errors += 1
        if reply["error"].get("status") == 422:
            outcome.invalid_requests += 1
    return json.dumps(reply)
