from pathlib import Path

import pytest
from pydantic import ValidationError

from pacds.config import load_config

SAMPLE = """
llm:
  base_url: "${LLM_URL}"
  model: fake
  api_key: "${LLM_KEY}"
auth:
  issuers:
    - issuer: https://issuer.test
      audience: pacds
clients:
  - subject: system:serviceaccount:support:triage-agent
    repos: ["git.example.com/shop/*"]
git:
  cache_dir: /tmp/pacds-cache
logs:
  allowed_hosts: ["*.s3.amazonaws.com"]
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def test_loads_config_and_expands_env(tmp_path):
    config = load_config(write(tmp_path, SAMPLE), env={"LLM_URL": "http://llm.test/v1", "LLM_KEY": "k"})
    assert config.llm.base_url == "http://llm.test/v1"
    assert config.llm.api_key == "k"
    assert config.llm.max_turns == 30
    assert config.engine_name == "pacds-1"
    assert config.clients[0].repos == ["git.example.com/shop/*"]
    assert config.logs.allow_http is False
    assert config.logs.allow_private_ips is False
    assert config.limits.max_concurrent_evaluations == 4


def test_empty_session_header_means_none(tmp_path):
    text = SAMPLE.replace("  model: fake\n", '  model: fake\n  session_header: "${HDR}"\n')
    config = load_config(write(tmp_path, text), env={"LLM_URL": "u", "LLM_KEY": "k", "HDR": ""})
    assert config.llm.session_header is None
    config = load_config(write(tmp_path, text), env={"LLM_URL": "u", "LLM_KEY": "k", "HDR": "x-opencode-session"})
    assert config.llm.session_header == "x-opencode-session"


def test_unset_env_var_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="LLM_KEY"):
        load_config(write(tmp_path, SAMPLE), env={"LLM_URL": "http://llm.test/v1"})


def test_unknown_keys_are_rejected(tmp_path):
    with pytest.raises(ValidationError):
        load_config(write(tmp_path, SAMPLE + "\nsurprise: true\n"), env={"LLM_URL": "u", "LLM_KEY": "k"})


def test_llm_api_defaults_to_chat_completions(tmp_path):
    config = load_config(write(tmp_path, SAMPLE), env={"LLM_URL": "u", "LLM_KEY": "k"})
    assert config.llm.api == "chat_completions"


@pytest.mark.parametrize(("value", "expected"), [("responses", "responses"), ("chat_completions", "chat_completions"), ("", "chat_completions")])
def test_llm_api_is_configurable(tmp_path, value, expected):
    text = SAMPLE.replace("  model: fake\n", '  model: fake\n  api: "${API}"\n')
    config = load_config(write(tmp_path, text), env={"LLM_URL": "u", "LLM_KEY": "k", "API": value})
    assert config.llm.api == expected


def test_unknown_llm_api_is_rejected(tmp_path):
    text = SAMPLE.replace("  model: fake\n", "  model: fake\n  api: completions\n")
    with pytest.raises(ValidationError):
        load_config(write(tmp_path, text), env={"LLM_URL": "u", "LLM_KEY": "k"})


def test_output_token_limit_is_optional(tmp_path):
    text = SAMPLE.replace("  model: fake\n", '  model: fake\n  max_output_tokens: "${MAXOUT}"\n')
    assert load_config(write(tmp_path, text), env={"LLM_URL": "u", "LLM_KEY": "k", "MAXOUT": ""}).llm.max_output_tokens is None
    assert load_config(write(tmp_path, text), env={"LLM_URL": "u", "LLM_KEY": "k", "MAXOUT": "16000"}).llm.max_output_tokens == 16000


def test_traces_are_off_by_default(tmp_path):
    config = load_config(write(tmp_path, SAMPLE), env={"LLM_URL": "u", "LLM_KEY": "k"})
    assert config.trace.dir is None


def test_trace_dir_alone_is_refused(tmp_path):
    with pytest.raises(ValidationError, match="development"):
        load_config(write(tmp_path, SAMPLE + "trace:\n  dir: /traces\n"), env={"LLM_URL": "u", "LLM_KEY": "k"})


def test_trace_dir_is_accepted_for_development(tmp_path):
    text = SAMPLE + "trace:\n  dir: ${TRACE_DIR}\n  enabled_for: development\n"
    config = load_config(write(tmp_path, text), env={"LLM_URL": "u", "LLM_KEY": "k", "TRACE_DIR": "/traces"})
    assert config.trace.dir == Path("/traces")
    # An empty directory leaves traces off, so the dev config works with and without them.
    assert load_config(write(tmp_path, text), env={"LLM_URL": "u", "LLM_KEY": "k", "TRACE_DIR": ""}).trace.dir is None


def test_dev_config_enables_traces_only_through_the_environment():
    dev = Path(__file__).parents[2] / "dev" / "pacds.yaml"
    env = {name: "" for name in ("LLM_SESSION_HEADER", "LLM_API", "LLM_MAX_OUTPUT_TOKENS", "PACDS_TRACE_DIR")}
    env.update(LLM_BASE_URL="http://fake-llm:8000/v1", LLM_MODEL="fake", LLM_API_KEY="k")
    assert load_config(dev, env=env).trace.dir is None
    assert load_config(dev, env={**env, "PACDS_TRACE_DIR": "/traces"}).trace.dir == Path("/traces")
