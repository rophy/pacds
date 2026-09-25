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
