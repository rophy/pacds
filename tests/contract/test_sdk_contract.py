import threading
import time
from pathlib import Path

import pytest
import uvicorn
from typesafe_sdk import (
    Choice,
    Noul,
    RetryPolicy,
    Score,
    TypeSafeAuthenticationError,
    TypeSafeClient,
    TypeSafePermissionDeniedError,
    TypeSafeUnprocessableEntityError,
)

from pacds.app import Services, create_app
from pacds.audit import AuditLogger
from pacds.auth import TokenVerifier
from pacds.config import AuthConfig, ClientConfig, Config, GitConfig, IssuerConfig, LLMConfig
from pacds.devtools import fake_llm
from pacds.engine.evaluator import Evaluator
from pacds.workspace.git import Checkout
from pacds.workspace.logs import LogFetcher
from tests.conftest import AUDIENCE, ISSUER, SUBJECT

GIT = {"url": "https://git.example.com/shop/checkout.git", "ref": "main"}


class Server:
    def __init__(self, app):
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> str:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.01)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=10)


class LocalGit:
    def __init__(self, path: Path):
        self.path = path

    async def checkout(self, url: str, ref: str) -> Checkout:
        return Checkout(path=self.path, sha="b" * 40)


@pytest.fixture(scope="module")
def pacds_url(tmp_path_factory, jwks):
    root = tmp_path_factory.mktemp("contract")
    (root / "repo").mkdir()
    (root / "repo" / "app.py").write_text("print('hi')\n")
    with Server(fake_llm.create_app()) as llm_url:

        async def static_jwks(issuer):
            return jwks

        config = Config(
            llm=LLMConfig(base_url=f"{llm_url}/v1", model="fake", api_key="k", max_turns=5, time_budget_seconds=30),
            auth=AuthConfig(issuers=[IssuerConfig(issuer=ISSUER, audience=AUDIENCE)]),
            clients=[ClientConfig(subject=SUBJECT, repos=["git.example.com/shop/*"])],
            git=GitConfig(cache_dir=root / "cache"),
            work_dir=root / "work",
        )
        with open(root / "audit.log", "w") as audit_stream:
            services = Services(
                config=config,
                verifier=TokenVerifier(config.auth.issuers, static_jwks),
                git=LocalGit(root / "repo"),
                logs=LogFetcher(config.logs),
                engine=Evaluator(config.llm),
                audit=AuditLogger(audit_stream),
            )
            with Server(create_app(services)) as url:
                yield url


def client(url: str, token: str) -> TypeSafeClient:
    return TypeSafeClient(api_key=token, base_url=url, timeout=60, retry=RetryPolicy(max_retries=0))


def test_all_question_types_round_trip(pacds_url, make_token):
    response = client(pacds_url, make_token()).system_one(
        state={"pacds": {"git": GIT}, "user_report": "Checkout fails with 'payment declined'"},
        questions={
            "cause": Choice(instructions="What caused this issue?", criteria={"code_defect": None, "user_action": None, "user_environment": None}),
            "urgent": Noul(instructions="Is this urgent?"),
            "severity": Score(instructions="How severe?", criteria=["low", "medium", "high"]),
        },
    )
    assert response.model == "pacds-1"
    assert response.answers["cause"].choice in {"code_defect", "user_action", "user_environment"}
    assert 0 <= response.answers["urgent"].noul <= 1
    assert 0 <= response.answers["severity"].score <= 2
    assert response.usage.input_tokens > 0


def test_models_list(pacds_url, make_token):
    models = client(pacds_url, make_token()).models.list()
    assert [model.name for model in models.models] == ["pacds-1"]


def test_bad_token_raises_authentication_error(pacds_url, make_token):
    with pytest.raises(TypeSafeAuthenticationError):
        client(pacds_url, make_token({"aud": "kubernetes"})).system_one(state={"pacds": {"git": GIT}}, questions={"q": Noul(instructions="?")})


def test_unauthorized_repo_raises_permission_denied(pacds_url, make_token):
    with pytest.raises(TypeSafePermissionDeniedError):
        client(pacds_url, make_token()).system_one(
            state={"pacds": {"git": {"url": "https://git.example.com/secret/vault.git", "ref": "main"}}},
            questions={"q": Noul(instructions="?")},
        )


def test_missing_pacds_state_raises_unprocessable(pacds_url, make_token):
    with pytest.raises(TypeSafeUnprocessableEntityError):
        client(pacds_url, make_token()).system_one(state={"user_report": "hi"}, questions={"q": Noul(instructions="?")})
