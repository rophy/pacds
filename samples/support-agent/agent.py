"""A minimal support agent that uses PACDS. Usage: python agent.py ticket.json

Reads a ticket, lets an LLM triage it with the skills in ./skills, and lets the LLM
ask PACDS questions about the ticket's code version and logs. Prints the decision as JSON.
Environment: LLM_BASE_URL, LLM_MODEL, LLM_API_KEY, PACDS_URL, and either PACDS_TOKEN or
PACDS_OIDC_TOKEN_URL + PACDS_OIDC_CLIENT_ID + PACDS_OIDC_CLIENT_SECRET.
"""

import json
import os
import re
import sys
from pathlib import Path

import httpx
import openai

SKILLS = Path(__file__).parent / "skills"
MAX_TURNS, MAX_PACDS_CALLS = 10, 3

TOOLS = [
    {"type": "function", "function": {
        "name": "call_pacds",
        "description": "Ask PACDS questions about the application's code and logs. See the pacds skill.",
        "parameters": {"type": "object", "properties": {
            "document": {"type": "object", "description": 'Context for PACDS, e.g. {"user_report": "..."}'},
            "logs": {"type": "array", "items": {"type": "string"}, "description": 'Names of the ticket\'s log files to include, e.g. ["server.log"]'},
            "questions": {"type": "object", "description": "Questions keyed by id (noul, choice or score)"},
        }, "required": ["document", "logs", "questions"]}}},
    {"type": "function", "function": {
        "name": "submit_decision",
        "description": "Submit your triage decision for this ticket. Ends the ticket.",
        "parameters": {"type": "object", "properties": {
            "class": {"type": "string", "enum": ["A", "B", "C", "D"]},
            "escalate": {"type": "boolean", "description": "Escalate to the development team"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reply": {"type": "string", "description": "The reply to send to the user"},
        }, "required": ["class", "escalate", "confidence", "reply"]}}},
]


def system_prompt(repo):
    name = re.sub(r"\.git$", "", repo.rstrip("/")).rsplit("/", 1)[-1]
    application = " ".join(word.capitalize() for word in name.split("-"))
    skills = [(SKILLS / skill / "SKILL.md").read_text() for skill in ("tech-support", "pacds")]
    return (f"You handle support tickets for {application}.\n"
            "Use your skills below. Submit a decision for every ticket.\n\n" + "\n\n---\n\n".join(skills))


def pacds_token(http):
    if os.environ.get("PACDS_TOKEN"):
        return os.environ["PACDS_TOKEN"]
    response = http.post(os.environ["PACDS_OIDC_TOKEN_URL"], data={
        "grant_type": "client_credentials",
        "client_id": os.environ["PACDS_OIDC_CLIENT_ID"],
        "client_secret": os.environ["PACDS_OIDC_CLIENT_SECRET"]})
    response.raise_for_status()
    return response.json()["access_token"]


def call_pacds(http, ticket, arguments):
    """One Jev request. The model names log files; the URLs come from the ticket."""
    names = arguments.get("logs") or []
    known = {log["name"]: log for log in ticket.get("logs", [])}
    if unknown := [n for n in names if n not in known]:
        return {"error": f"no log named {', '.join(map(str, unknown))}; this ticket has: {', '.join(known) or 'none'}"}
    body = {
        "model": "pacds",
        "state": {**arguments.get("document", {}),
                  "pacds": {"git": {"url": ticket["repo"], "ref": ticket["ref"]}, "logs": [known[n] for n in names]}},
        "questions": arguments.get("questions"),
    }
    response = http.post(os.environ["PACDS_URL"].rstrip("/") + "/v1/systemone", json=body,
                         headers={"Authorization": f"Bearer {pacds_token(http)}"}, timeout=600)
    return response.json()  # answers, or an error the model can react to


def ticket_message(ticket):
    parts = [f"New ticket.\n\n## User report\n\n{ticket['report']}"]
    if ticket.get("logs"):
        parts.append("## Attached logs (pass their names to call_pacds)\n\n" + ", ".join(log["name"] for log in ticket["logs"]))
    return "\n\n".join(parts)


def triage(ticket, llm, http):
    """Returns the decision dict, or None when the model never submits one."""
    messages = [{"role": "system", "content": system_prompt(ticket["repo"])},
                {"role": "user", "content": ticket_message(ticket)}]
    pacds_calls = 0
    for _ in range(MAX_TURNS):
        reply = llm.chat.completions.create(model=os.environ["LLM_MODEL"], messages=messages, tools=TOOLS).choices[0].message
        messages.append(reply.model_dump(exclude_none=True))
        if not reply.tool_calls:
            messages.append({"role": "user", "content": "Use your tools; finish with submit_decision."})
            continue
        for call in reply.tool_calls:
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = None
            if call.function.name == "submit_decision" and isinstance(arguments, dict) and arguments.get("class") in ("A", "B", "C", "D"):
                return arguments
            if not isinstance(arguments, dict):
                result = {"error": "arguments must be a JSON object"}
            elif call.function.name != "call_pacds":
                result = {"error": f"invalid call to {call.function.name}: class must be one of A, B, C, D"}
            elif pacds_calls >= MAX_PACDS_CALLS:
                result = {"error": f"call_pacds limit of {MAX_PACDS_CALLS} reached; decide with what you have"}
            else:
                pacds_calls += 1
                result = call_pacds(http, ticket, arguments)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)})
    return None


def main(argv, llm=None, http=None):
    ticket = json.loads(Path(argv[0]).read_text())
    llm = llm or openai.OpenAI(base_url=os.environ.get("LLM_BASE_URL"), api_key=os.environ.get("LLM_API_KEY", "none"))
    decision = triage(ticket, llm, http or httpx.Client())
    if decision is None:
        print("no decision reached", file=sys.stderr)
        return 1
    print(json.dumps(decision))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
