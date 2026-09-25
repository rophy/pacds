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


@pytest.mark.parametrize(("status", "expected"), [(529, 529), (503, 529), (400, 500)])
async def test_llm_errors_are_mapped(tools, status, expected):
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json={"error": {"message": "x"}}))
    with pytest.raises(PacdsError) as error:
        await Evaluator(LLM, client=client_for(transport)).evaluate({}, QUESTIONS, tools)
    assert error.value.status == expected


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
