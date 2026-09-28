"""Fetch a repository at one commit, with limited earlier history, into a cache keyed by URL and commit SHA."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import stat
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from pacds.config import GitConfig
from pacds.errors import PacdsError

logger = logging.getLogger(__name__)

_SHA = re.compile(r"[0-9a-f]{40}")
_ASKPASS = """#!/bin/sh
case "$1" in
  Username*) printf '%s\\n' "$PACDS_GIT_USERNAME" ;;
  *) printf '%s\\n' "$PACDS_GIT_PASSWORD" ;;
esac
"""


@dataclass(frozen=True)
class Checkout:
    path: Path
    sha: str


_PASSTHROUGH = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy")


class GitFetcher:
    def __init__(
        self,
        config: GitConfig,
        *,
        env: Mapping[str, str] = os.environ,
        allowed_protocols: tuple[str, ...] = ("https",),
        ca_bundle: Path | None = None,
    ) -> None:
        self._config = config
        self._ca_bundle = ca_bundle
        self._env = env
        self._protocols = allowed_protocols
        self._locks: dict[Path, asyncio.Lock] = {}
        self._home = Path(tempfile.mkdtemp(prefix="pacds-git-"))
        self._askpass = self._home / "askpass.sh"
        self._askpass.write_text(_ASKPASS)
        self._askpass.chmod(stat.S_IRWXU)

    async def checkout(self, url: str, ref: str) -> Checkout:
        env = self._git_env(url)
        sha = ref.lower() if _SHA.fullmatch(ref.lower()) else await self._resolve(url, ref, env=env)
        dest = self._config.cache_dir / hashlib.sha256(url.encode()).hexdigest()[:16] / f"{sha}-h{self._config.history_depth}"
        lock = self._locks.setdefault(dest, asyncio.Lock())
        async with lock:
            if not dest.exists():
                await self._fetch(url, sha, dest, env=env)
        return Checkout(path=dest, sha=sha)

    async def _resolve(self, url: str, ref: str, *, env: dict[str, str] | None = None) -> str:
        output = await self._git(
            ["ls-remote", url, f"refs/heads/{ref}", f"refs/tags/{ref}", f"refs/tags/{ref}^{{}}"],
            env=env if env is not None else self._git_env(url),
            code="git_unavailable",
        )
        refs: dict[str, str] = {}
        for line in output.splitlines():
            sha, _, name = line.partition("\t")
            refs[name] = sha
        for name in (f"refs/tags/{ref}^{{}}", f"refs/tags/{ref}", f"refs/heads/{ref}"):
            if name in refs:
                return refs[name]
        raise PacdsError(422, "unknown_ref", f"ref {ref!r} was not found in the repository")

    async def _fetch(self, url: str, sha: str, dest: Path, *, env: dict[str, str]) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        staging = dest.parent / f".{sha}.{uuid.uuid4().hex}"
        try:
            staging.mkdir()
            await self._git(["init", "--quiet", str(staging)], env=env)
            await self._git(["-C", str(staging), "fetch", "--quiet", "--depth", str(self._config.history_depth + 1), url, sha], env=env, code="git_fetch_failed")
            await self._git(["-C", str(staging), "checkout", "--quiet", "FETCH_HEAD"], env=env)
            limit = self._config.max_repo_size_mb * 1024 * 1024
            if _tree_size(staging) > limit:
                raise PacdsError(422, "repo_too_large", f"repository is larger than {self._config.max_repo_size_mb} MB")
            staging.rename(dest)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    async def _git(self, args: list[str], *, env: dict[str, str], code: str = "git_error") -> str:
        command = ["git", "-c", "protocol.allow=never", "-c", "credential.helper="]
        for protocol in self._protocols:
            command += ["-c", f"protocol.{protocol}.allow=always"]
        process = await asyncio.create_subprocess_exec(
            *command,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self._config.timeout_seconds)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise PacdsError(502, "git_timeout", "git operation timed out") from None
        if process.returncode != 0:
            operation = args[2] if args[0] == "-C" else args[0]
            logger.warning("git %s failed: %s", operation, stderr.decode(errors="replace")[-500:])
            raise PacdsError(502, code, "git operation failed")
        return stdout.decode()

    def _git_env(self, url: str) -> dict[str, str]:
        env = {
            "PATH": self._env.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self._home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            # A corporate proxy and CA reach git through the environment, not through a global git config.
            **{name: self._env[name] for name in _PASSTHROUGH if name in self._env},
            **({"GIT_SSL_CAINFO": str(self._ca_bundle)} if self._ca_bundle else {}),
            "GIT_ASKPASS": str(self._askpass),
        }
        host = (urlsplit(url).hostname or "").lower()
        credential = next((c for c in self._config.credentials if c.host.lower() == host), None)
        if credential is not None:
            token = self._env.get(credential.token_env)
            if not token:
                raise PacdsError(500, "git_credentials_missing", "git credentials are not configured")
            env["PACDS_GIT_USERNAME"] = credential.username
            env["PACDS_GIT_PASSWORD"] = token
        return env


def _tree_size(root: Path) -> int:
    return sum(path.lstat().st_size for path in root.rglob("*") if path.is_file() and not path.is_symlink())
