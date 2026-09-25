"""Which client may ask about which git repository."""

from __future__ import annotations

import re
from functools import lru_cache
from urllib.parse import urlsplit

from pacds.config import ClientConfig


def normalize_git_url(url: str) -> str:
    """Return `host[:port]/path` for an https git URL, or raise ValueError."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ValueError("git url must use https")
    if parts.username is not None or parts.password is not None:
        raise ValueError("git url must not contain credentials")
    if parts.query or parts.fragment:
        raise ValueError("git url must not have a query or fragment")
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError("git url must have a host")
    host_port = f"{host}:{parts.port}" if parts.port else host
    segments = [segment for segment in parts.path.split("/") if segment]
    if not segments or any(segment in (".", "..") for segment in segments):
        raise ValueError("git url has an invalid path")
    if segments[-1].endswith(".git"):
        segments[-1] = segments[-1][: -len(".git")]
    if not segments[-1]:
        raise ValueError("git url has an invalid path")
    return "/".join([host_port, *segments])


@lru_cache(maxsize=256)
def _pattern(pattern: str) -> re.Pattern[str]:
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**", index):
            parts.append(".*")
            index += 2
        elif pattern[index] == "*":
            parts.append("[^/]*")
            index += 1
        else:
            parts.append(re.escape(pattern[index]))
            index += 1
    return re.compile("".join(parts))


def is_authorized(subject: str, git_url: str, clients: list[ClientConfig]) -> bool:
    try:
        target = normalize_git_url(git_url)
    except ValueError:
        return False
    return any(
        client.subject == subject and any(_pattern(repo).fullmatch(target) for repo in client.repos)
        for client in clients
    )
