import asyncio

import httpx
import openai
import pytest
from typesafe_sdk import Choice, ChoiceAnswer, Noul, NoulAnswer, Score, ScoreAnswer

from pacds.config import LLMConfig
from pacds.devtools.fake_llm import create_app, fill
from pacds.engine.evaluator import Evaluator
from pacds.engine.tools import WorkspaceTools
from pacds.errors import PacdsError

QUESTIONS = {
    "cause": Choice(instructions="What caused it?", criteria={"code_defect": None, "user_action": None, "user_environment": None}),
    "urgent": Noul(instructions="Urgent?"),
    "severity": Score(instructions="How bad?", criteria=["low", "medium", "high"]),
}
LLM = LLMConfig(base_url="http://llm.test/v1", model="fake", api_key="k", max_turns=5, time_budget_seconds=10)


@pytest.fixture
def tools(tmp_path) -> WorkspaceTools:
    (tmp_path / "repo").mkdir()
    (tmp_path / "logs").mkdir()
    return WorkspaceTools(tmp_path / "repo", tmp_path / "logs")


def client_for(transport: httpx.AsyncBaseTransport) -> openai.AsyncOpenAI:
    return openai.AsyncOpenAI(base_url=LLM.base_url, api_key="k", max_retries=0, http_client=httpx.AsyncClient(transport=transport))


def test_fill_produces_uniform_probability_maps():
    schema = {
        "$defs": {"P": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}}},
        "type": "object",
        "properties": {"answers": {"type": "object", "properties": {"q": {"$ref": "#/$defs/P"}, "n": {"type": "number"}, "s": {"type": "string"}}}},
    }
    assert fill(schema) == {"answers": {"q": {"a": 0.5, "b": 0.5}, "n": 0.5, "s": ""}}


async def test_evaluates_with_fake_llm(tools):
    evaluator = Evaluator(LLM, client=client_for(httpx.ASGITransport(app=create_app())))
    evaluation = await evaluator.evaluate({"user_report": "broken"}, QUESTIONS, tools)
    assert isinstance(evaluation.answers["cause"], ChoiceAnswer)
    assert isinstance(evaluation.answers["urgent"], NoulAnswer)
    assert isinstance(evaluation.answers["severity"], ScoreAnswer)
    assert evaluation.answers["severity"].score == pytest.approx(1.0)
    assert evaluation.input_tokens > 0 and evaluation.output_tokens > 0


async def test_session_header_is_stable_within_an_evaluation(tools):
    fake = create_app()
    seen: list[str | None] = []

    class Recording(httpx.ASGITransport):
        async def handle_async_request(self, request):
            seen.append(request.headers.get("x-opencode-session"))
            return await super().handle_async_request(request)

    llm = LLM.model_copy(update={"session_header": "x-opencode-session"})
    evaluator = Evaluator(llm, client=client_for(Recording(app=fake)))
    await evaluator.evaluate({}, QUESTIONS, tools)
    first = set(seen)
    seen.clear()
    await evaluator.evaluate({}, QUESTIONS, tools)
    assert len(first) == 1 and None not in first
    assert len(set(seen)) == 1 and set(seen) != first


async def test_no_session_header_by_default(tools):
    seen: list[str | None] = []

    class Recording(httpx.ASGITransport):
        async def handle_async_request(self, request):
            seen.append(request.headers.get("x-opencode-session"))
            return await super().handle_async_request(request)

    await Evaluator(LLM, client=client_for(Recording(app=create_app()))).evaluate({}, QUESTIONS, tools)
    assert set(seen) == {None}


@pytest.mark.parametrize(("status", "expected"), [(529, 529), (503, 529), (400, 500)])
async def test_llm_errors_are_mapped(tools, status, expected, caplog):
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json={"error": {"message": "upstream says no"}}))
    with pytest.raises(PacdsError) as error:
        await Evaluator(LLM, client=client_for(transport)).evaluate({}, QUESTIONS, tools)
    assert error.value.status == expected
    assert "upstream says no" not in error.value.message
    assert "upstream says no" in caplog.text


async def test_long_retry_after_fails_fast_as_529(tools):
    quota = {"type": "error", "error": {"type": "GoUsageLimitError", "message": "Go usage limit exceeded"}}
    transport = httpx.MockTransport(lambda request: httpx.Response(429, headers={"retry-after": "14066"}, json=quota))
    with pytest.raises(PacdsError) as error:
        async with asyncio.timeout(5):
            await Evaluator(LLM, client=client_for(transport)).evaluate({}, QUESTIONS, tools)
    assert (error.value.status, error.value.code) == (529, "overloaded")


async def test_short_retry_after_is_retried(tools):
    fake = httpx.ASGITransport(app=create_app())
    calls = 0

    class RateLimitedOnce(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(429, headers={"retry-after-ms": "10"}, json={"error": {"message": "slow down"}})
            return await fake.handle_async_request(request)

    evaluation = await Evaluator(LLM, client=client_for(RateLimitedOnce())).evaluate({}, QUESTIONS, tools)
    assert isinstance(evaluation.answers["cause"], ChoiceAnswer)
    assert calls > 1


async def test_responses_api_is_used_when_configured(tools):
    paths: list[str] = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(400, json={"error": {"message": "stop here"}})

    llm = LLM.model_copy(update={"api": "responses"})
    with pytest.raises(PacdsError):
        await Evaluator(llm, client=client_for(httpx.MockTransport(handler))).evaluate({}, QUESTIONS, tools)
    assert paths == ["/v1/responses"]


async def test_turn_budget_is_504(tools):
    looping = {
        "id": "c", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "list_files", "arguments": "{}"}}]}}],
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=looping))
    with pytest.raises(PacdsError) as error:
        await Evaluator(LLM, client=client_for(transport)).evaluate({}, QUESTIONS, tools)
    assert (error.value.status, error.value.code) == (504, "agent_budget_exceeded")


async def test_malformed_answers_are_500(tools):
    def handler(request):
        body = request.read()
        if b"response_format" in body:
            message = {"role": "assistant", "content": "not json"}
        else:
            message = {"role": "assistant", "content": "done"}
        return httpx.Response(200, json={"id": "c", "object": "chat.completion", "created": 0, "model": "m", "choices": [{"index": 0, "finish_reason": "stop", "message": message}]})

    with pytest.raises(PacdsError) as error:
        await Evaluator(LLM, client=client_for(httpx.MockTransport(handler))).evaluate({}, QUESTIONS, tools)
    assert (error.value.status, error.value.code) == (500, "malformed_answer")


async def test_llm_timeout_is_529_not_budget_504(tools):
    def hang(request):
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(PacdsError) as error:
        await Evaluator(LLM, client=client_for(httpx.MockTransport(hang))).evaluate({}, QUESTIONS, tools)
    assert (error.value.status, error.value.code) == (529, "overloaded")


def test_claude_code_needs_no_http_clients():
    from pacds.config import LLMConfig
    from pacds.engine.evaluator import Evaluator

    evaluator = Evaluator(LLMConfig.model_validate({"model": "haiku", "api": "claude_code"}))
    assert evaluator._client is None and evaluator._anthropic is None


@pytest.fixture
def git_tools(tmp_path) -> WorkspaceTools:
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "logs").mkdir()
    (repo / "app.py").write_text("x = 1\n")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    for args in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "init"]):
        subprocess.run([*git, *args], check=True)
    return WorkspaceTools(repo, tmp_path / "logs")


def _claude_evaluator(fake_claude, streams, **llm):
    log = fake_claude(streams)
    return Evaluator(LLMConfig.model_validate({"model": "haiku", "api": "claude_code", "max_turns": 4, **llm})), log


CHOICE = {"q": Choice(instructions="Is it a defect?", criteria={"yes": None, "no": None})}


async def test_claude_code_usage_limit_is_not_retried(fake_claude, git_tools):
    # A usage limit lasts until its reset: the 529 carries a long Retry-After, so the adapter fails fast instead of retrying.
    # (A retried request would still re-investigate in full: test_a_request_after_a_failed_one_investigates_again.)
    evaluator, log = _claude_evaluator(fake_claude, "usage_limit.jsonl,probabilities.jsonl")
    with pytest.raises(PacdsError) as error:
        await evaluator.evaluate({"user_report": "broken"}, CHOICE, git_tools)
    assert (error.value.status, error.value.code) == (529, "overloaded")
    assert len(log.read_text().splitlines()) == 1


async def test_claude_code_usage_limit_is_overloaded(fake_claude, git_tools):
    evaluator, _ = _claude_evaluator(fake_claude, "usage_limit.jsonl")
    with pytest.raises(PacdsError) as error:
        await evaluator.evaluate({"user_report": "broken"}, CHOICE, git_tools)
    assert (error.value.status, error.value.code) == (529, "overloaded")


async def test_claude_code_auth_failure_is_an_engine_error(fake_claude, git_tools):
    evaluator, _ = _claude_evaluator(fake_claude, "auth.jsonl")
    with pytest.raises(PacdsError) as error:
        await evaluator.evaluate({"user_report": "broken"}, CHOICE, git_tools)
    assert (error.value.status, error.value.code) == (500, "engine_error")


async def test_claude_code_time_budget_is_a_504(fake_claude, git_tools):
    evaluator, _ = _claude_evaluator(fake_claude, "hang", time_budget_seconds=1)
    with pytest.raises(PacdsError) as error:
        await evaluator.evaluate({"user_report": "broken"}, CHOICE, git_tools)
    assert (error.value.status, error.value.code) == (504, "agent_budget_exceeded")
