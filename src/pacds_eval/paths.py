"""Where the toolkit keeps run directories and finds a deployed PACDS's traces (the image sets these; see the runbook)."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

TRACES_MOUNT = Path("/traces")


def runs_root() -> Path:
    """$PACDS_RUNS_DIR, else ./eval-runs."""
    return Path(os.environ.get("PACDS_RUNS_DIR") or "eval-runs")


def default_run_dir(explicit: str | None = None) -> Path:
    """--run-dir, else $EVAL_RUN_DIR, else <runs root>/<UTC time>."""
    return Path(explicit or os.environ.get("EVAL_RUN_DIR") or runs_root() / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}")


def trace_source_dir() -> str | None:
    """$PACDS_TRACE_SOURCE_DIR, else /traces when it is a mounted directory, else None (no traces to collect)."""
    return os.environ.get("PACDS_TRACE_SOURCE_DIR") or (str(TRACES_MOUNT) if TRACES_MOUNT.is_dir() else None)
