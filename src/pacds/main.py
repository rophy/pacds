"""Service entry point."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import uvicorn

from pacds.app import Services, create_app
from pacds.audit import AuditLogger
from pacds.auth import TokenVerifier, fetch_jwks
from pacds.config import Config, load_config
from pacds.engine.evaluator import Evaluator
from pacds.workspace.git import GitFetcher
from pacds.workspace.logs import LogFetcher


def build_services(config: Config) -> Services:
    return Services(
        config=config,
        verifier=TokenVerifier(config.auth.issuers, fetch_jwks),
        git=GitFetcher(config.git),
        logs=LogFetcher(config.logs),
        engine=Evaluator(config.llm),
        audit=AuditLogger(),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    config = load_config(Path(os.environ.get("PACDS_CONFIG", "/etc/pacds/config.yaml")))
    uvicorn.run(
        create_app(build_services(config)),
        host=os.environ.get("PACDS_HOST", "0.0.0.0"),
        port=int(os.environ.get("PACDS_PORT", "8080")),
    )
