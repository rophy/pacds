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


def run_calls(tmp_path, calls, pacds, monkeypatch, decide=True):
    env(monkeypatch)
    submit = completion(("submit_decision", {"class": "A", "escalate": False, "confidence": 1, "reply": "r"}))
    seen = []
    llm = llm_client([completion(*calls), submit], seen)
    code = load().main([ticket(tmp_path)], llm=llm, http=httpx.Client(transport=httpx.MockTransport(pacds)))
    return code, [json.loads(m["content"]) for m in seen[1]["messages"] if m["role"] == "tool"]


def ok(request):
    return httpx.Response(200, json={"answers": {}})


def test_pacds_call_limit(tmp_path, monkeypatch):
    sent = []
    args = {"document": {}, "logs": [], "questions": {}}

    def pacds(request):
        sent.append(request)
        return ok(request)

    code, results = run_calls(tmp_path, [("call_pacds", args)] * 4, pacds, monkeypatch)
    assert code == 0 and len(sent) == 3
    assert "limit" in results[3]["error"]


def test_unknown_log_and_tool(tmp_path, monkeypatch):
    sent = []
    code, results = run_calls(tmp_path, [("call_pacds", {"document": {}, "logs": ["nope.log"], "questions": {}}), ("frobnicate", {})],
                              lambda r: sent.append(r) or ok(r), monkeypatch)
    assert sent == [] and "server.log" in results[0]["error"]["message"]
    assert results[1] == {"error": "unknown tool frobnicate"}


def test_gateway_html_and_transport_error_go_to_model(tmp_path, monkeypatch):
    args = {"document": {}, "logs": [], "questions": {}}
    code, results = run_calls(tmp_path, [("call_pacds", args)], lambda r: httpx.Response(502, text="<html>Bad Gateway</html>"), monkeypatch)
    assert code == 0 and results[0]["error"]["status"] == 502 and "Bad Gateway" in results[0]["error"]["message"]

    def boom(request):
        raise httpx.ConnectTimeout("slow")

    code, results = run_calls(tmp_path, [("call_pacds", args)], boom, monkeypatch)
    assert code == 0 and results[0]["error"]["status"] is None


def test_non_dict_document(tmp_path, monkeypatch):
    sent = []
    args = {"document": "just text", "logs": [], "questions": {}}
    code, _ = run_calls(tmp_path, [("call_pacds", args)], lambda r: sent.append(r) or ok(r), monkeypatch)
    assert code == 0 and list(json.loads(sent[0].content)["state"]) == ["pacds"]


def test_oidc_token_fetched_once(tmp_path, monkeypatch):
    monkeypatch.delenv("PACDS_TOKEN", raising=False)
    monkeypatch.setenv("PACDS_OIDC_TOKEN_URL", "http://idp.corp.example/token")
    monkeypatch.setenv("PACDS_OIDC_CLIENT_ID", "cid")
    monkeypatch.setenv("PACDS_OIDC_CLIENT_SECRET", "csecret")
    seen = []

    def transport(request):
        seen.append(request)
        if request.url.host == "idp.corp.example":
            return httpx.Response(200, json={"access_token": "tok"})
        return ok(request)

    args = {"document": {}, "logs": [], "questions": {}}
    monkeypatch.setenv("LLM_MODEL", "m")
    monkeypatch.setenv("PACDS_URL", "http://pacds.corp.example")
    submit = completion(("submit_decision", {"class": "A", "escalate": False, "confidence": 1, "reply": "r"}))
    llm = llm_client([completion(("call_pacds", args), ("call_pacds", args)), submit], [])
    assert load().main([ticket(tmp_path)], llm=llm, http=httpx.Client(transport=httpx.MockTransport(transport))) == 0
    tokens = [r for r in seen if r.url.host == "idp.corp.example"]
    assert len(tokens) == 1 and b"grant_type=client_credentials" in tokens[0].content and b"client_id=cid" in tokens[0].content
    assert [r.headers["authorization"] for r in seen if r.url.host != "idp.corp.example"] == ["Bearer tok"] * 2


def test_token_failure_exits_cleanly(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("PACDS_TOKEN", raising=False)
    monkeypatch.delenv("PACDS_OIDC_TOKEN_URL", raising=False)
    monkeypatch.setenv("LLM_MODEL", "m")
    monkeypatch.setenv("PACDS_URL", "http://pacds.corp.example")
    assert load().main([ticket(tmp_path)], llm=llm_client([], []), http=httpx.Client()) == 2
    assert "PACDS token" in capsys.readouterr().err


def test_missing_environment_is_reported_upfront(tmp_path, monkeypatch, capsys):
    for name in ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "PACDS_URL", "PACDS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert load().main([ticket(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert "LLM_MODEL" in err and "PACDS_URL" in err and "LLM_BASE_URL" in err and "LLM_API_KEY" in err
    assert "Traceback" not in err
