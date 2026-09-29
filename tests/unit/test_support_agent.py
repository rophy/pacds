import json

import httpx
import openai

from tests.replay.harness import Case
from tests.support_agent.agent import SKILLS_DIR, application_name, load_skill, run_agent, ticket_message

CASE = Case(id="c1", truth="B", repo="https://github.com/usememos/memos.git", ref="v1.0.0", report="Pinning does nothing", logs=["issue.log"])
LOGS = {"issue.log": "WARN something\n"}
SIGNED: list[str] = []


def attachment_url(name: str) -> str:
    SIGNED.append(name)
    return f"https://s3.test/replay/c1/{name}?sig=fresh"


def completion(message: dict, finish: str = "stop") -> dict:
    return {"id": "c", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "finish_reason": finish, "message": message}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


def tool_call(name: str, arguments: dict, call_id: str = "t1") -> dict:
    return completion({"role": "assistant", "content": None, "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}, "tool_calls")


DECIDE = tool_call("submit_decision", {"class": "B", "escalate": False, "confidence": 0.8}, "t9")
QUESTIONS = {"deliberate": {"type": "noul", "instructions": "Is it deliberate?"}}


class ScriptedLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def client(self) -> openai.AsyncOpenAI:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(json.loads(request.content))
            return httpx.Response(200, json=self.responses.pop(0) if len(self.responses) > 1 else self.responses[0])

        return openai.AsyncOpenAI(base_url="http://llm.test/v1", api_key="k", max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


class FakePacds:
    def __init__(self, reply):
        self.reply = reply
        self.calls: list[dict] = []

    async def __call__(self, case: Case, body: dict) -> dict:
        self.calls.append(body)
        return self.reply


def run(llm, pacds, **kwargs):
    return run_agent(CASE, log_texts=LOGS, attachment_url=attachment_url, client=llm.client(), model="m", pacds=pacds, **kwargs)


def test_skills_exist_and_stay_separate():
    support, pacds = load_skill("tech-support"), load_skill("pacds")
    assert "A" in support and "D" in support and "PACDS" not in support
    assert "call_pacds" in pacds and "user_error" not in pacds
    assert (SKILLS_DIR / "tech-support" / "SKILL.md").is_file()


def test_application_name_comes_from_the_repo():
    assert application_name("https://github.com/louislam/uptime-kuma.git") == "Uptime Kuma"
    assert application_name("https://github.com/go-gitea/gitea.git") == "Gitea"


def test_ticket_carries_report_log_text_and_attachment_names_but_no_repository_or_urls():
    message = ticket_message(CASE, LOGS)
    assert "Pinning does nothing" in message and "WARN something" in message and "issue.log" in message
    assert "github.com" not in message and "v1.0.0" not in message and "http" not in message


async def test_agent_calls_pacds_with_bound_git_then_decides():
    llm = ScriptedLLM([tool_call("call_pacds", {"document": {"user_report": "x"}, "logs": ["issue.log"], "questions": QUESTIONS}), DECIDE])
    pacds = FakePacds({"answers": {"deliberate": {"type": "noul", "noul": 0.9}}})
    outcome = await run(llm, pacds)
    assert outcome.decision == "B" and outcome.escalate is False and outcome.confidence == 0.8
    [body] = pacds.calls
    assert body["state"]["pacds"] == {"git": {"url": CASE.repo, "ref": CASE.ref},
                                      "logs": [{"name": "issue.log", "url": "https://s3.test/replay/c1/issue.log?sig=fresh"}]}
    assert body["state"]["user_report"] == "x" and body["questions"] == QUESTIONS
    tool_result = llm.requests[1]["messages"][-1]
    assert tool_result["role"] == "tool" and '"noul": 0.9' in tool_result["content"]
    assert outcome.pacds_requests == [{"questions": QUESTIONS, "logs": ["issue.log"], "request_id": None,
                                       "reply": {"answers": {"deliberate": {"type": "noul", "noul": 0.9}}}}]


async def test_trace_keeps_the_conversation_and_pacds_request_ids():
    llm = ScriptedLLM([tool_call("call_pacds", {"document": {}, "logs": [], "questions": QUESTIONS}), DECIDE])
    outcome = await run(llm, FakePacds({"answers": {}, "request_id": "req-1"}))
    assert outcome.pacds_requests[0]["request_id"] == "req-1"
    assert "req-1" not in llm.requests[1]["messages"][-1]["content"]
    assert [m["role"] for m in outcome.transcript] == ["user", "assistant", "tool", "assistant"]
    assert "Pinning does nothing" in outcome.transcript[0]["content"]
    assert outcome.transcript[-1]["tool_calls"][0]["function"]["name"] == "submit_decision"


async def test_a_failing_llm_keeps_the_partial_trace():
    llm = ScriptedLLM([tool_call("call_pacds", {"document": {}, "logs": [], "questions": QUESTIONS}), {"not": "a completion"}])
    outcome = await run(llm, FakePacds({"answers": {}}))
    assert outcome.decision is None and outcome.error
    assert len(outcome.pacds_requests) == 1 and [m["role"] for m in outcome.transcript] == ["user", "assistant", "tool"]


def test_output_redacts_presigned_signatures():
    from tests.support_agent.run import redact

    text = json.dumps({"url": "http://s3:9000/logs/a.log?X-Amz-Credential=c&X-Amz-Signature=abc123&X-Amz-Date=d"})
    assert "abc123" not in redact(text) and "X-Amz-Signature=REDACTED&X-Amz-Date=d" in redact(text)


async def test_system_prompt_holds_both_skills_and_no_repository():
    llm = ScriptedLLM([DECIDE])
    await run(llm, FakePacds({}))
    system = llm.requests[0]["messages"][0]["content"]
    assert load_skill("tech-support") in system and load_skill("pacds") in system
    assert "Memos" in system and "github.com" not in system


async def test_pacds_errors_are_returned_to_the_agent():
    llm = ScriptedLLM([tool_call("call_pacds", {"document": {}, "logs": [], "questions": {}}), DECIDE])
    outcome = await run(llm, FakePacds({"error": {"status": 422, "code": "invalid_request", "message": "questions must be a non-empty object"}}))
    assert "invalid_request" in llm.requests[1]["messages"][-1]["content"]
    assert outcome.decision == "B" and outcome.invalid_requests == 1


async def test_pacds_call_limit_is_enforced():
    call = tool_call("call_pacds", {"document": {}, "logs": [], "questions": QUESTIONS})
    llm = ScriptedLLM([call, call, call, DECIDE])
    pacds = FakePacds({"answers": {}})
    outcome = await run(llm, pacds, max_pacds_calls=2)
    assert len(pacds.calls) == 2
    assert "limit" in llm.requests[3]["messages"][-1]["content"]
    assert outcome.decision == "B"


async def test_no_decision_within_the_turn_limit():
    llm = ScriptedLLM([completion({"role": "assistant", "content": "thinking"})])
    outcome = await run(llm, FakePacds({}), max_turns=3)
    assert outcome.decision is None and outcome.turns == 3


async def test_without_pacds_there_is_no_pacds_tool_or_skill():
    llm = ScriptedLLM([DECIDE])
    await run_agent(CASE, log_texts=LOGS, client=llm.client(), model="m", pacds=None)
    request = llm.requests[0]
    assert [tool["function"]["name"] for tool in request["tools"]] == ["submit_decision"]
    assert "call_pacds" not in request["messages"][0]["content"]


def test_scoring_counts_class_escalation_and_pacds_usage():
    from tests.support_agent.agent import Outcome
    from tests.support_agent.run import score_outcomes

    cases = {c: Case(id=c, truth=t, repo="u", ref="r", report="x") for c, t in (("d1", "D"), ("b1", "B"), ("c1", "C"))}
    outcomes = [
        ("d1", Outcome(decision="D", escalate=True, pacds_requests=[{}], input_tokens=100)),
        ("b1", Outcome(decision="D", escalate=True, pacds_requests=[{}, {}], invalid_requests=1, input_tokens=50)),
        ("c1", Outcome(decision=None)),
    ]
    summary = score_outcomes(cases, outcomes)
    assert summary["class_accuracy"] == 1 / 3
    assert summary["escalation_accuracy"] == 1 / 3   # b1 escalated wrongly, c1 undecided
    assert summary["undecided"] == 1
    assert summary["pacds_calls_per_ticket"] == 1.0
    assert summary["invalid_requests"] == 1
    assert summary["confusion"]["B"]["D"] == 1


async def test_unknown_attachment_names_are_refused_without_calling_pacds():
    llm = ScriptedLLM([tool_call("call_pacds", {"document": {}, "logs": ["other.log"], "questions": QUESTIONS}), DECIDE])
    pacds = FakePacds({"answers": {}})
    outcome = await run(llm, pacds)
    assert pacds.calls == [] and outcome.pacds_requests == []
    assert "no attachment named other.log; this ticket has: issue.log" in llm.requests[1]["messages"][-1]["content"]


async def test_log_urls_from_the_model_are_not_accepted():
    llm = ScriptedLLM([tool_call("call_pacds", {"document": {}, "logs": [{"name": "issue.log", "url": "https://evil.test/x"}],
                                                "questions": QUESTIONS}), DECIDE])
    pacds = FakePacds({"answers": {}})
    await run(llm, pacds)
    assert pacds.calls == [] and "logs must be a list of attachment names" in llm.requests[1]["messages"][-1]["content"]


async def test_urls_are_made_only_for_attachments_the_agent_uses():
    SIGNED.clear()
    llm = ScriptedLLM([tool_call("call_pacds", {"document": {}, "logs": [], "questions": QUESTIONS}), DECIDE])
    await run(llm, FakePacds({"answers": {}}))
    assert SIGNED == []


async def test_model_calls_are_traced_and_linked_to_pacds_requests():
    from pacds.engine.trace import Trace, messages_at

    llm = ScriptedLLM([tool_call("call_pacds", {"document": {}, "logs": [], "questions": QUESTIONS}), DECIDE])
    trace = Trace(case_id="c1")
    outcome = await run(llm, FakePacds({"answers": {}, "request_id": "req-1"}), trace=trace)
    assert outcome.decision == "B"
    assert [call["phase"] for call in trace.calls] == ["agent", "agent"]
    for n, sent in enumerate(llm.requests, start=1):
        assert messages_at(trace.calls, n) == sent["messages"]
    [tool] = trace.tools
    assert tool["name"] == "call_pacds" and tool["pacds_request_id"] == "req-1" and tool["call"] == 1
    assert trace.usage()["input"] == outcome.input_tokens == 20


CALL = 'call_pacds:{"document": {"user_report": "r"}, "logs": [], "questions": {"q": {"kind": "noul"}}}'


async def _stub_pacds(case, body):
    return {"answers": {"q": "yes"}, "request_id": "req-1"}


async def test_claude_code_ticket(fake_claude):
    fake_claude("support.jsonl", tool=CALL)
    seen = []

    async def pacds(case, body):
        seen.append(body)
        return {"answers": {"q": "yes"}, "request_id": "req-1"}
    outcome = await run_agent(CASE, log_texts={}, client=None, model="haiku", pacds=pacds, api="claude_code")
    assert (outcome.decision, outcome.escalate, outcome.confidence) == ("B", True, 0.8)
    assert outcome.pacds_requests[0]["request_id"] == "req-1" and seen[0]["state"]["pacds"]["git"]["url"] == CASE.repo
    assert outcome.transcript[0]["role"] == "user"
    assert outcome.transcript[1]["tool_calls"][0]["function"]["name"] == "call_pacds"
    assert outcome.transcript[2]["role"] == "tool"
    assert outcome.turns == 3 and outcome.input_tokens == 1857 and outcome.error is None


async def test_claude_code_without_pacds_replays_and_with_pacds_runs_live(fake_claude):
    from pacds.engine.replay import Recordings
    from pacds.engine.trace import Trace

    log = fake_claude("support.jsonl")
    trace = Trace(case_id="c1", repeat=1, variant="no-pacds", model="haiku")
    await run_agent(CASE, log_texts={}, client=None, model="haiku", pacds=None, api="claude_code", trace=trace)
    lines = len(log.read_text().splitlines())
    recordings = Recordings([trace.to_dict()])
    again = await run_agent(CASE, log_texts={}, client=None, model="haiku", pacds=None, api="claude_code", replay=recordings)
    assert again.decision == "B" and len(log.read_text().splitlines()) == lines

    recordings = Recordings([trace.to_dict()])
    await run_agent(CASE, log_texts={}, client=None, model="haiku", pacds=_stub_pacds, api="claude_code", replay=recordings)
    assert len(log.read_text().splitlines()) == lines + 1
