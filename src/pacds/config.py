"""PACDS configuration: one YAML file with ${ENV_VAR} substitution."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LLMConfig(_Strict):
    base_url: str
    model: str
    api_key: str
    max_turns: int = Field(default=30, ge=1)
    time_budget_seconds: float = Field(default=180, gt=0)
    # Header carrying one ID per evaluation, for providers that route by session (e.g. x-opencode-session).
    session_header: str | None = None
    # Wire protocol: some models (e.g. OpenAI GPT on OpenCode Go) are only served on /responses.
    api: Literal["chat_completions", "responses", "anthropic"] = "chat_completions"
    # Output token limit per model call; unset uses the provider's default, which can be too low for
    # reasoning models that think before answering.
    max_output_tokens: int | None = Field(default=None, gt=0)
    # Thinking depth on the Anthropic Messages API (output_config.effort); unset uses the model's default.
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    # Seconds per model call; self-hosted servers on busy GPUs can need more than hosted APIs.
    timeout_seconds: float = Field(default=120, gt=0)
    # Answer with a JSON-schema response format (guided decoding on vLLM); false puts the schema in the prompt
    # instead, for servers or models that reject response_format, or reject it together with tools.
    structured_outputs: bool = True
    # Extra request fields passed through to the server as-is, e.g. vLLM's
    # {"chat_template_kwargs": {"enable_thinking": true}} or {"reasoning_effort": "high"}.
    extra_body: dict[str, Any] = {}
    # The final answer request carries the investigation's tools (tool_choice none), so its prompt starts like the
    # investigation's and the server's prefix cache covers it. false drops them, for servers that reject tool_choice none.
    final_keeps_tools: bool = True
    # Context budget for one investigation, in prompt tokens: above it the oldest tool results are replaced by a short
    # note (down to two thirds of it), keeping the latest ones. Unset keeps every result: prefix caching makes resent
    # results cheap, so this is for context windows smaller than an investigation's largest request (60K measured).
    context_budget_tokens: int | None = Field(default=None, ge=8000)

    @field_validator("max_output_tokens", "effort", "context_budget_tokens", mode="before")
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        return value or None

    @field_validator("session_header")
    @classmethod
    def _empty_is_none(cls, value: str | None) -> str | None:
        return value or None

    @field_validator("api", mode="before")
    @classmethod
    def _empty_is_default(cls, value: str | None) -> str:
        return value or "chat_completions"


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
    # Commits of history before the deployed commit, so an investigation can tell a regression from a design
    # decision. Only earlier commits: a later fix is never visible. 0 fetches the deployed commit alone.
    history_depth: int = Field(default=500, ge=0)


class LogsConfig(_Strict):
    allowed_hosts: list[str] = []
    max_file_size_mb: int = Field(default=50, gt=0)
    max_files: int = Field(default=10, ge=0)
    fetch_timeout_seconds: float = Field(default=30, gt=0)
    allow_http: bool = False
    allow_private_ips: bool = False
    # Hosts (patterns like allowed_hosts) that may resolve to private addresses, e.g. an internal S3/MinIO, while
    # every other host must stay public. Narrower than allow_private_ips.
    private_hosts: list[str] = []


class TlsConfig(_Strict):
    # PEM file with the corporate CA(s), trusted in addition to the system CAs for every outbound connection:
    # the LLM, git, log storage and OIDC discovery.
    ca_file: Path | None = None

    @field_validator("ca_file", mode="before")
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        return value or None


class LimitsConfig(_Strict):
    max_concurrent_evaluations: int = Field(default=4, ge=1)


class TraceConfig(_Strict):
    """Per-request investigation traces for evaluation analysis. Traces hold source code and log content, so a
    directory is accepted only together with enabled_for: development: a production config cannot turn them on
    by setting one value."""

    dir: Path | None = None
    enabled_for: Literal["development"] | None = None
    # Directory of recorded traces: identical model requests get the recorded response (pacds.engine.replay).
    replay_from: Path | None = None

    @field_validator("dir", "enabled_for", "replay_from", mode="before")
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        return value or None

    @model_validator(mode="after")
    def _development_only(self) -> TraceConfig:
        for name in ("dir", "replay_from"):
            if getattr(self, name) is not None and self.enabled_for != "development":
                raise ValueError(f"trace.{name} requires trace.enabled_for: development; traces contain source code and logs")
        return self


class Config(_Strict):
    engine_name: str = "pacds-1"
    llm: LLMConfig
    auth: AuthConfig
    clients: list[ClientConfig] = []
    git: GitConfig
    logs: LogsConfig = LogsConfig()
    limits: LimitsConfig = LimitsConfig()
    trace: TraceConfig = TraceConfig()
    tls: TlsConfig = TlsConfig()
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
