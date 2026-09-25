"""PACDS configuration: one YAML file with ${ENV_VAR} substitution."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LLMConfig(_Strict):
    base_url: str
    model: str
    api_key: str
    max_turns: int = Field(default=30, ge=1)
    time_budget_seconds: float = Field(default=180, gt=0)


class IssuerConfig(_Strict):
    issuer: str
    audience: str
    jwks_uri: str | None = None
    ca_file: str | None = None
    token_file: str | None = None


class AuthConfig(_Strict):
    issuers: list[IssuerConfig] = Field(min_length=1)


class ClientConfig(_Strict):
    subject: str
    repos: list[str]


class GitCredential(_Strict):
    host: str
    username: str = "x-access-token"
    token_env: str


class GitConfig(_Strict):
    credentials: list[GitCredential] = []
    cache_dir: Path
    max_repo_size_mb: int = Field(default=500, gt=0)
    timeout_seconds: float = Field(default=120, gt=0)


class LogsConfig(_Strict):
    allowed_hosts: list[str] = []
    max_file_size_mb: int = Field(default=50, gt=0)
    max_files: int = Field(default=10, ge=0)
    fetch_timeout_seconds: float = Field(default=30, gt=0)
    allow_http: bool = False
    allow_private_ips: bool = False


class LimitsConfig(_Strict):
    max_concurrent_evaluations: int = Field(default=4, ge=1)


class Config(_Strict):
    engine_name: str = "pacds-1"
    llm: LLMConfig
    auth: AuthConfig
    clients: list[ClientConfig] = []
    git: GitConfig
    logs: LogsConfig = LogsConfig()
    limits: LimitsConfig = LimitsConfig()
    work_dir: Path = Path("/tmp/pacds")


_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_env(text: str, env: Mapping[str, str]) -> str:
    """Replace ${NAME} with env[NAME]; an unset variable is a configuration error."""

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in env:
            raise ValueError(f"config references unset environment variable {name}")
        return env[name]

    return _ENV_REF.sub(substitute, text)


def load_config(path: Path, env: Mapping[str, str] | None = None) -> Config:
    text = expand_env(Path(path).read_text(), os.environ if env is None else env)
    return Config.model_validate(yaml.safe_load(text))
