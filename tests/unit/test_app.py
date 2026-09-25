import asyncio
import io
import json
from pathlib import Path

import httpx
import pytest
from typesafe_sdk import ChoiceAnswer

from pacds.app import REQUEST_ID_HEADER, Services, create_app
from pacds.audit import AuditLogger
from pacds.config import AuthConfig, ClientConfig, Config, GitConfig, IssuerConfig, LimitsConfig, LLMConfig
from pacds.engine.evaluator import Evaluation
from pacds.errors import PacdsError
from pacds.workspace.git import Checkout

SUBJECT = "system:serviceaccount:support:triage-agent"
SHA = "a" * 40
BODY = {
    "model": "jev-latest",
    "state": {
        "pacds": {
            "git": {"url": "https://git.example.com/shop/checkout.git", "ref": "main"},
            "logs": [{"name": "server.log", "url": "https://bucket.s3.amazonaws.com/server.log?X-Amz-Signature=secret"}],
        },
        "user_report": "Checkout fails",
    },
    "questions": {"cause": {"type": "choice", "instructions": "Cause?", "criteria": {"code_defect": None, "user_action": None}}},
}
GOOD = Evaluation(
    answers={"cause": ChoiceAnswer(choice="code_defect", confidence=0.5, probabilities={"code_defect": 0.75, "user_action": 0.25})},
    input_tokens=100,
    output_tokens=20,
)
AUTH = {"Authorization": "Bearer good"}


class FakeVerifier:
    async def verify(self, token: str) -> str:
        if token != "good":
            raise PacdsError(401, "unauthorized", "invalid or missing bearer token")
        return SUBJECT


class FakeGit:
    def __init__(self, root: Path):
        self.root = root

    async def checkout(self, url: str, ref: str) -> Checkout:
        return Checkout(path=self.root, sha=SHA)


class FakeLogs:
    async def fetch_all(self, logs, dest: Path) -> None:
        for log in logs:
            (dest / log.name).write_text("ERROR\n")


class FakeEngine:
    def __init__(self, result=GOOD, gate: asyncio.Event | None = None):
        self.result = result
        self.gate = gate
        self.seen = None

    async def evaluate(self, state, questions, tools):
        self.seen = state
        if self.gate is not None:
            await self.gate.wait()
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def make(tmp_path, engine=None, limit=4):
    config = Config(
        llm=LLMConfig(base_url="http://llm.test/v1", model="fake", api_key="k"),
        auth=AuthConfig(issuers=[IssuerConfig(issuer="https://issuer.test", audience="pacds")]),
        clients=[ClientConfig(subject=SUBJECT, repos=["git.example.com/shop/*"])],
        git=GitConfig(cache_dir=tmp_path / "cache"),
        limits=LimitsConfig(max_concurrent_evaluations=limit),
        work_dir=tmp_path / "work",
    )
    (tmp_path / "repo").mkdir(exist_ok=True)
    audit = io.StringIO()
    engine = engine or FakeEngine()
    services = Services(
        config=config, verifier=FakeVerifier(), git=FakeGit(tmp_path / "repo"), logs=FakeLogs(), engine=engine, audit=AuditLogger(audit)
    )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(services)), base_url="http://pacds.test")
    return client, audit, engine


def audit_records(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


async def test_happy_path(tmp_path):
    client, audit, engine = make(tmp_path)
    response = await client.post("/v1/systemone", json=BODY, headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {
        "model": "pacds-1",
        "answers": {"cause": {"type": "choice", "choice": "code_defect", "confidence": 0.5, "probabilities": {"code_defect": 0.75, "user_action": 0.25}}},
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }
    assert response.headers[REQUEST_ID_HEADER]
    assert engine.seen == {"user_report": "Checkout fails"}
    [record] = audit_records(audit)
    assert record["event"] == "pacds.audit"
    assert record["subject"] == SUBJECT
    assert record["git_sha"] == SHA
    assert record["log_hosts"] == ["bucket.s3.amazonaws.com"]
    assert record["status"] == 200
    assert record["request_id"] == response.headers[REQUEST_ID_HEADER]
    assert "secret" not in audit.getvalue()
    assert not list((tmp_path / "work").iterdir())


async def test_models(tmp_path):
    client, _, _ = make(tmp_path)
    response = await client.get("/v1/models", headers=AUTH)
    assert response.json() == {"models": [{"name": "pacds-1", "description": "PACDS incident triage over source code and logs", "release_date": "2026-09-25"}]}
    assert (await client.get("/v1/models")).status_code == 401


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer bad"}, {"Authorization": "Basic good"}])
async def test_unauthenticated(tmp_path, headers):
    client, audit, _ = make(tmp_path)
    response = await client.post("/v1/systemone", json=BODY, headers=headers)
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "unauthorized"
    assert audit_records(audit)[0]["status"] == 401


async def test_invalid_request(tmp_path):
    client, _, _ = make(tmp_path)
    response = await client.post("/v1/systemone", json={"model": "x", "state": "hi", "questions": {}}, headers=AUTH)
    assert response.status_code == 422
    response = await client.post("/v1/systemone", content=b"not json", headers=AUTH)
    assert response.status_code == 422


async def test_forbidden_repo(tmp_path):
    client, audit, _ = make(tmp_path)
    body = json.loads(json.dumps(BODY))
    body["state"]["pacds"]["git"]["url"] = "https://git.example.com/secret/vault.git"
    response = await client.post("/v1/systemone", json=body, headers=AUTH)
    assert response.status_code == 403
    assert audit_records(audit)[0]["error"] == "forbidden"


async def test_concurrency_limit(tmp_path):
    gate = asyncio.Event()
    client, _, _ = make(tmp_path, engine=FakeEngine(gate=gate), limit=1)
    first = asyncio.create_task(client.post("/v1/systemone", json=BODY, headers=AUTH))
    await asyncio.sleep(0.05)
    second = await client.post("/v1/systemone", json=BODY, headers=AUTH)
    assert second.status_code == 429
    gate.set()
    assert (await first).status_code == 200


async def test_engine_error_propagates(tmp_path):
    client, audit, _ = make(tmp_path, engine=FakeEngine(result=PacdsError(504, "agent_budget_exceeded", "stopped")))
    response = await client.post("/v1/systemone", json=BODY, headers=AUTH)
    assert response.status_code == 504
    assert audit_records(audit)[0]["status"] == 504


async def test_invalid_engine_answer_is_blocked(tmp_path):
    leaky = Evaluation(
        answers={"cause": {"type": "choice", "choice": "applyDiscount()", "confidence": 1, "probabilities": {"code_defect": 1, "user_action": 0}}},
        input_tokens=1,
        output_tokens=1,
    )
    client, _, _ = make(tmp_path, engine=FakeEngine(result=leaky))
    response = await client.post("/v1/systemone", json=BODY, headers=AUTH)
    assert response.status_code == 500
    assert "applyDiscount" not in response.text
