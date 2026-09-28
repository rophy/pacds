"""HTTP layer: TypeSafe's System One wire contract."""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from pacds.audit import AuditLogger, AuditRecord
from pacds.authz import is_authorized
from pacds.config import Config
from pacds.context import request_id as current_request_id
from pacds.engine.evaluator import Evaluation
from pacds.engine.tools import WorkspaceTools
from pacds.engine.trace import Trace
from pacds.errors import PacdsError
from pacds.request import LogSource, ParsedRequest, parse_request
from pacds.validate import validate_answers
from pacds.workspace.git import Checkout

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "x-typesafe-request-id"
RELEASE_DATE = "2026-09-25"


class Verifier(Protocol):
    async def verify(self, token: str) -> str: ...


class Git(Protocol):
    async def checkout(self, url: str, ref: str) -> Checkout: ...


class Logs(Protocol):
    async def fetch_all(self, logs: list[LogSource], dest: Path) -> None: ...


class Engine(Protocol):
    async def evaluate(self, state: dict[str, Any], questions: dict[str, Any], tools: WorkspaceTools, trace: Trace | None = None) -> Evaluation: ...


@dataclass
class Services:
    config: Config
    verifier: Verifier
    git: Git
    logs: Logs
    engine: Engine
    audit: AuditLogger


def create_app(services: Services) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    config = services.config
    active = 0

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        request.state.request_id = uuid.uuid4().hex
        token = current_request_id.set(request.state.request_id)
        try:
            response = await call_next(request)
        finally:
            current_request_id.reset(token)
        response.headers[REQUEST_ID_HEADER] = request.state.request_id
        return response

    @app.exception_handler(PacdsError)
    async def pacds_error(request: Request, error: PacdsError) -> JSONResponse:
        return JSONResponse(error.body(), status_code=error.status)

    async def authenticate(request: Request) -> str:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise PacdsError(401, "unauthorized", "invalid or missing bearer token")
        return await services.verifier.verify(token.strip())

    @app.get("/v1/models")
    async def list_models(request: Request) -> dict[str, Any]:
        await authenticate(request)
        return {
            "models": [
                {"name": config.engine_name, "description": "PACDS incident triage over source code and logs", "release_date": RELEASE_DATE}
            ]
        }

    @app.post("/v1/systemone")
    async def system_one(request: Request) -> dict[str, Any]:
        nonlocal active
        record = AuditRecord(request_id=request.state.request_id)
        started = time.monotonic()
        trace = Trace(request_id=record.request_id, started=datetime.now(UTC).isoformat()) if config.trace.dir else None
        try:
            record.subject = await authenticate(request)
            try:
                body = await request.json()
            except ValueError:
                raise PacdsError(422, "invalid_request", "request body must be JSON") from None
            parsed = parse_request(body, max_logs=config.logs.max_files)
            record.git_url = parsed.inputs.git.url
            record.git_ref = parsed.inputs.git.ref
            record.log_hosts = sorted({urlsplit(log.url).hostname or "" for log in parsed.inputs.logs})
            record.questions = {qid: question.model_dump(mode="json") for qid, question in parsed.questions.items()}
            if trace is not None:
                trace.info.update(subject=record.subject, document=parsed.context, questions=record.questions,
                                  git={"url": record.git_url, "ref": record.git_ref, "history_depth": config.git.history_depth})
            if not is_authorized(record.subject, parsed.inputs.git.url, config.clients):
                raise PacdsError(403, "forbidden", "not authorized for this repository")
            if active >= config.limits.max_concurrent_evaluations:
                raise PacdsError(429, "rate_limited", "too many concurrent evaluations; retry later")
            active += 1
            try:
                evaluation, checkout = await _evaluate(parsed, trace)
            finally:
                active -= 1
            record.git_sha = checkout.sha
            answers = validate_answers(parsed.questions, evaluation.answers)
            usage = {"input_tokens": evaluation.input_tokens, "output_tokens": evaluation.output_tokens}
            record.answers = answers
            record.usage = usage
            return {"model": config.engine_name, "answers": answers, "usage": usage}
        except PacdsError as error:
            record.status = error.status
            record.error = error.code
            raise
        except Exception:
            record.status = 500
            record.error = "internal_error"
            raise
        finally:
            record.latency_ms = round((time.monotonic() - started) * 1000)
            services.audit.write(record)
            if trace is not None:
                _write_trace(trace, record)

    def _write_trace(trace: Trace, record: AuditRecord) -> None:
        info = trace.info
        info.setdefault("git", {})["sha"] = record.git_sha
        info["final"] = {"raw": info.pop("final_raw", None), "answers": info.pop("answers", None), "validated": record.answers}
        info.update(status=record.status, error=record.error, latency_ms=record.latency_ms, audit_usage=record.usage)
        trace_dir = config.trace.dir or Path()
        try:
            trace_dir.mkdir(parents=True, exist_ok=True)
            (trace_dir / f"{record.request_id}.json").write_text(json.dumps(trace.to_dict(), default=str))
        except OSError as error:
            logger.warning("could not write the trace: %s", error)

    async def _evaluate(parsed: ParsedRequest, trace: Trace | None) -> tuple[Evaluation, Checkout]:
        config.work_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=config.work_dir) as tmp:
            logs_dir = Path(tmp) / "logs"
            logs_dir.mkdir()
            checkout, fetched = await asyncio.gather(
                services.git.checkout(parsed.inputs.git.url, parsed.inputs.git.ref),
                services.logs.fetch_all(parsed.inputs.logs, logs_dir),
                return_exceptions=True,
            )
            for result in (checkout, fetched):
                if isinstance(result, BaseException):
                    raise result
            if trace is not None:
                fetched_logs = [logs_dir / log.name for log in parsed.inputs.logs]
                trace.info["logs"] = [{"name": path.name, "bytes": path.stat().st_size if path.is_file() else None} for path in fetched_logs]
            tools = WorkspaceTools(checkout.path, logs_dir)
            evaluation = await services.engine.evaluate(parsed.context, parsed.questions, tools, trace=trace)
        return evaluation, checkout

    return app
