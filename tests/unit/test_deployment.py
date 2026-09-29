"""The corporate deployment surface: TLS trust, vLLM knobs, health, and the shipped production config sample."""

import json
import ssl
from pathlib import Path

import httpx
import openai
import pytest
from pydantic import ValidationError

from pacds import tls
from pacds.config import LLMConfig, load_config
from pacds.engine.evaluator import Evaluator
from pacds.workspace.git import GitFetcher

ROOT = Path(__file__).parents[2]
SAMPLE = ROOT / "deploy" / "pacds.example.yaml"
CA = """-----BEGIN CERTIFICATE-----
MIIBszCCAVmgAwIBAgIUcorp
-----END CERTIFICATE-----
"""


def test_ca_bundle_adds_the_corporate_ca_to_the_system_ones(tmp_path):
    (tmp_path / "corp.pem").write_text(CA)
    bundle = tls.ca_bundle(tmp_path / "corp.pem", tmp_path / "work")
    text = bundle.read_text()
    assert text.rstrip().endswith("-----END CERTIFICATE-----") and "MIIBszCCAVmgAwIBAgIUcorp" in text
    system = ssl.get_default_verify_paths().cafile
    if system and Path(system).is_file():
        assert text.count("BEGIN CERTIFICATE") > 1
    assert tls.ca_bundle(None, tmp_path) is None and tls.context(None) is True


def test_a_ca_file_without_certificates_is_refused(tmp_path):
    (tmp_path / "empty.pem").write_text("nothing here")
    with pytest.raises(ValueError, match="PEM"):
        tls.ca_bundle(tmp_path / "empty.pem", tmp_path)


def test_git_gets_the_bundle_and_the_proxy(tmp_path):
    from pacds.config import GitConfig

    fetcher = GitFetcher(GitConfig(cache_dir=tmp_path), env={"PATH": "/usr/bin", "HTTPS_PROXY": "http://proxy:3128", "SECRET": "x"},
                         ca_bundle=tmp_path / "ca.pem")
    env = fetcher._git_env("https://git.corp.example/team/app.git")
    assert env["GIT_SSL_CAINFO"] == str(tmp_path / "ca.pem") and env["HTTPS_PROXY"] == "http://proxy:3128" and "SECRET" not in env


async def test_extra_body_and_timeout_reach_the_server(tmp_path):
    seen = []

    def handler(request):
        seen.append((json.loads(request.content), request.extensions.get("timeout")))
        return httpx.Response(200, json={"id": "c", "object": "chat.completion", "created": 0, "model": "m",
                                         "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": '{"answers": {}}'}}],
                                         "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    from pacds.engine.agent_provider import AgentProvider

    client = openai.AsyncOpenAI(base_url="http://vllm.test/v1", api_key="k", max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    provider = AgentProvider(model_name="m", client=client, tools=None, max_turns=1, time_budget_seconds=10,  # type: ignore[arg-type]
                             extra_body={"chat_template_kwargs": {"enable_thinking": True}})
    await provider._complete(messages=[{"role": "user", "content": "q"}])
    assert seen[0][0]["chat_template_kwargs"] == {"enable_thinking": True}


def test_structured_outputs_switch_reaches_the_adapter():
    on = Evaluator(LLMConfig(base_url="http://vllm.test/v1", model="m", api_key="k"))
    off = Evaluator(LLMConfig(base_url="http://vllm.test/v1", model="m", api_key="k", structured_outputs=False, timeout_seconds=600))
    assert on._adapter is not off._adapter and off._client.timeout == 600


async def test_health_needs_no_token(tmp_path):
    from tests.unit.test_app import make

    client, _, _ = make(tmp_path)
    response = await client.get("/healthz")
    assert response.status_code == 200 and response.json() == {"status": "ok"}


ENV = {"PACDS_LLM_BASE_URL": "http://vllm:8000/v1", "PACDS_LLM_MODEL": "qwen", "PACDS_LLM_API_KEY": "k", "PACDS_CA_FILE": "",
       "PACDS_OIDC_ISSUER": "https://sso.corp.example/realms/it", "PACDS_GIT_TOKEN_ENV": "PACDS_GIT_TOKEN"}


def test_the_production_sample_loads_and_keeps_traces_off():
    config = load_config(SAMPLE, env=ENV)
    assert config.trace.dir is None and config.trace.replay_from is None
    assert config.llm.api == "chat_completions" and not config.logs.allow_private_ips and not config.logs.allow_http
    from pacds.authz import is_authorized

    assert is_authorized("support-agent", "https://git.corp.example/crm/app.git", config.clients)  # group/repo paths
    assert not is_authorized("support-agent", "https://git.other.example/crm/app.git", config.clients)


def test_the_production_sample_cannot_enable_traces_by_one_setting(tmp_path):
    text = SAMPLE.read_text() + "\ntrace:\n  dir: /var/lib/pacds/traces\n"
    (tmp_path / "config.yaml").write_text(text)
    with pytest.raises(ValidationError, match="development"):
        load_config(tmp_path / "config.yaml", env=ENV)
