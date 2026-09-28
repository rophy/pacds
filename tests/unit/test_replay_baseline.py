import json

import httpx
import openai

from tests.replay.baseline import BaselineProvider, baseline_state, evaluate_baseline
from tests.replay.harness import CLASSES, Case


def completion(content: str) -> dict:
    return {
        "id": "c", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


ANSWER = {"answers": {"cause": {"other_system": 0.1, "user_error": 0.1, "infrastructure": 0.7, "bug": 0.1}}}


def client_recording(requests: list) -> openai.AsyncOpenAI:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=completion(json.dumps(ANSWER)))

    return openai.AsyncOpenAI(base_url="http://llm.test/v1", api_key="k", max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_state_inlines_the_case_logs(tmp_path):
    (tmp_path / "c1").mkdir()
    (tmp_path / "c1" / "issue.log").write_text("permission denied\n")
    case = Case(id="c1", truth="C", repo="u", ref="v1", report="Backup fails", logs=["issue.log"])
    assert baseline_state(case, tmp_path) == {"user_report": "Backup fails", "logs": {"issue.log": "permission denied\n"}}


def test_state_without_logs_has_only_the_report(tmp_path):
    case = Case(id="c1", truth="C", repo="u", ref="v1", report="Backup fails", logs=[])
    assert baseline_state(case, tmp_path) == {"user_report": "Backup fails"}


async def test_baseline_answers_in_one_request_without_tools(tmp_path):
    requests: list = []
    case = Case(id="c1", truth="C", repo="u", ref="v1", report="Backup fails", logs=[])
    result = await evaluate_baseline(case, tmp_path, client=client_recording(requests), model="m")
    assert len(requests) == 1
    assert "tools" not in requests[0]
    assert "response_format" in requests[0]
    system = requests[0]["messages"][0]["content"]
    assert "no access" in system and "search_code" not in system
    assert (result.predicted, result.correct) == ("C", True)
    assert result.p_truth == 0.7


def test_baseline_provider_never_investigates():
    assert BaselineProvider.__mro__[1].__name__ == "AgentProvider"
    assert set(CLASSES) == {"A", "B", "C", "D"}


async def test_baseline_answer_is_traced(tmp_path):
    from pacds.engine.trace import Trace

    trace = Trace()
    case = Case(id="c1", truth="C", repo="u", ref="v1", report="Backup fails", logs=[])
    await evaluate_baseline(case, tmp_path, client=client_recording([]), model="m", trace=trace)
    [call] = trace.calls
    assert call["phase"] == "final" and json.loads(call["response"]["content"]) == ANSWER
    assert trace.info["final_raw"] == json.dumps(ANSWER)
