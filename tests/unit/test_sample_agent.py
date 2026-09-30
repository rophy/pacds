"""The standalone support-agent sample: runs against stub LLM and PACDS transports, no network."""

import importlib.util
import json
import re
from pathlib import Path

import httpx
import openai

SAMPLE = Path(__file__).parents[2] / "samples" / "support-agent"


def load():
    spec = importlib.util.spec_from_file_location("sample_agent", SAMPLE / "agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def completion(*calls):
    tool_calls = [{"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(calls)]
    message = {"role": "assistant", "content": None, "tool_calls": tool_calls}
    return {"id": "x", "object": "chat.completion", "created": 0, "model": "m", "choices": [{"index": 0, "finish_reason": "tool_calls", "message": message}]}


def llm_client(replies, seen):
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=replies.pop(0))
    return openai.OpenAI(base_url="http://llm.corp.example/v1", api_key="k", http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def ticket(tmp_path):
    path = tmp_path / "ticket.json"
    path.write_text(json.dumps({"report": "Login fails", "repo": "https://git.corp.example/acme/web-shop.git", "ref": "v1.2.3",
                                "logs": [{"name": "server.log", "url": "https://logs.corp.example/server.log?sig=1"}]}))
    return str(path)


def env(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "m")
    monkeypatch.setenv("PACDS_URL", "http://pacds.corp.example")
    monkeypatch.setenv("PACDS_TOKEN", "secret")


def test_consults_pacds_then_submits(tmp_path, monkeypatch, capsys):
    env(monkeypatch)
    asked = {"questions": {"q1": {"type": "noul", "instructions": "Is login broken?"}}, "document": {"user_report": "Login fails"}, "logs": ["server.log"]}
    decision = {"class": "B", "escalate": True, "confidence": 0.8, "reply": "Fixed in code."}
    seen, sent = [], []
    llm = llm_client([completion(("call_pacds", asked)), completion(("submit_decision", decision))], seen)

    def pacds(request):
        sent.append(request)
        return httpx.Response(200, json={"answers": {"q1": "yes"}})

    agent = load()
    code = agent.main([ticket(tmp_path)], llm=llm, http=httpx.Client(transport=httpx.MockTransport(pacds)))
    assert code == 0
    assert sent[0].url == "http://pacds.corp.example/v1/systemone"
    assert sent[0].headers["authorization"] == "Bearer secret"
    body = json.loads(sent[0].content)
    assert body["model"] == "pacds" and body["questions"] == asked["questions"]
    assert body["state"]["user_report"] == "Login fails"
    assert body["state"]["pacds"] == {"git": {"url": "https://git.corp.example/acme/web-shop.git", "ref": "v1.2.3"},
                                      "logs": [{"name": "server.log", "url": "https://logs.corp.example/server.log?sig=1"}]}
    assert json.loads(capsys.readouterr().out) == decision
    assert "Web Shop" in seen[0]["messages"][0]["content"] and "Login fails" in seen[0]["messages"][1]["content"]
    assert json.loads(seen[1]["messages"][-1]["content"]) == {"answers": {"q1": "yes"}}


def test_no_decision_exits_1(tmp_path, monkeypatch, capsys):
    env(monkeypatch)
    stop = {"id": "x", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "hm"}}]}
    assert load().main([ticket(tmp_path)], llm=llm_client([stop] * 10, []), http=httpx.Client()) == 1
    assert capsys.readouterr().out == ""


def test_sample_imports_nothing_from_pacds():
    source = (SAMPLE / "agent.py").read_text()
    assert not re.search(r"^\s*(from|import)\s+pacds", source, re.M)
    assert (SAMPLE / "skills" / "tech-support" / "SKILL.md").is_file()
