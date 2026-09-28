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
from pacds.context import request_id
from pacds.engine.evaluator import Evaluator
from pacds.engine.replay import Recordings
from pacds.workspace.git import GitFetcher
from pacds.workspace.logs import LogFetcher


def build_services(config: Config) -> Services:
    return Services(
        config=config,
        verifier=TokenVerifier(config.auth.issuers, fetch_jwks),
        git=GitFetcher(config.git),
        logs=LogFetcher(config.logs),
        engine=Evaluator(config.llm, replay=_recordings(config)),
        audit=AuditLogger(),
    )


def _recordings(config: Config) -> Recordings | None:
    if config.trace.replay_from is None:
        return None
    recordings = Recordings.from_dirs([config.trace.replay_from])
    logging.getLogger(__name__).info("replaying %d recorded model calls from %s", recordings.recorded, config.trace.replay_from)
    return recordings


class _DropQueryStrings(logging.Filter):
    """httpx logs full request URLs; a log URL's query string can be a credential (presigned URL)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_without_query(arg) for arg in record.args)
        return True


def _without_query(arg: object) -> object:
    # httpx and httpx2 each have their own URL type; match any URL-like argument by its text.
    if isinstance(arg, (int, float)):
        return arg
    text = str(arg)
    return text.split("?", 1)[0] if "://" in text else arg


REQUEST_LOGGERS = ("httpx", "httpx2")
# Every line carries the request it belongs to: clients see terse errors, the reasons are here.
LOG_FORMAT = "%(levelname)s:%(name)s:request=%(request_id)s:%(message)s"


class _RequestId(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id.get() or "-"
        return True


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, _RequestId) for f in handler.filters):
            handler.addFilter(_RequestId())
    for name in REQUEST_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, _DropQueryStrings) for f in logger.filters):
            logger.addFilter(_DropQueryStrings())


def main() -> None:
    configure_logging()
    config = load_config(Path(os.environ.get("PACDS_CONFIG", "/etc/pacds/config.yaml")))
    uvicorn.run(
        create_app(build_services(config)),
        host=os.environ.get("PACDS_LISTEN_HOST", "0.0.0.0"),
        port=int(os.environ.get("PACDS_LISTEN_PORT", "8080")),
    )
