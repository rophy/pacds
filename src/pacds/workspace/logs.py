"""Download caller-supplied log files (typically pre-signed object-storage URLs)."""

from __future__ import annotations

import asyncio
import fnmatch
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from pacds.config import LogsConfig
from pacds.errors import PacdsError
from pacds.request import LogSource

Resolver = Callable[[str, int], Awaitable[list[str]]]


async def resolve_host(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global


class LogFetcher:
    def __init__(
        self,
        config: LogsConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolve: Resolver = resolve_host,
    ) -> None:
        self._config = config
        self._transport = transport
        self._resolve = resolve

    async def fetch_all(self, logs: list[LogSource], dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        results = await asyncio.gather(*(self._fetch_one(source, dest) for source in logs), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def _fetch_one(self, source: LogSource, dest: Path) -> None:
        name = source.name
        try:
            url = httpx.URL(source.url)
        except httpx.InvalidURL:
            raise PacdsError(422, "invalid_request", f"log {name}: invalid url") from None
        schemes = {"https", "http"} if self._config.allow_http else {"https"}
        if url.scheme not in schemes:
            raise PacdsError(422, "invalid_request", f"log {name}: url must use https")
        host = url.host.lower()
        if not any(fnmatch.fnmatchcase(host, pattern.lower()) for pattern in self._config.allowed_hosts):
            raise PacdsError(422, "log_host_not_allowed", f"log {name}: host is not allowed")
        if not self._config.allow_private_ips:
            # Known limitation: the address is resolved again when connecting (DNS rebinding window).
            port = url.port or (443 if url.scheme == "https" else 80)
            try:
                addresses = await self._resolve(host, port)
            except OSError:
                raise PacdsError(502, "log_unreachable", f"log {name}: cannot resolve host") from None
            if not addresses or not all(_is_public(address) for address in addresses):
                raise PacdsError(422, "log_host_not_allowed", f"log {name}: host resolves to a non-public address")

        target = dest / name
        limit = self._config.max_file_size_mb * 1024 * 1024
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                follow_redirects=False,
                timeout=self._config.fetch_timeout_seconds,
                trust_env=False,
            ) as client:
                async with client.stream("GET", url) as response:
                    status = response.status_code
                    if 300 <= status < 400:
                        raise PacdsError(422, "log_redirect_refused", f"log {name}: redirects are not followed")
                    if 400 <= status < 500:
                        raise PacdsError(422, "log_fetch_rejected", f"log {name}: storage returned {status}")
                    if status >= 500:
                        raise PacdsError(502, "log_unreachable", f"log {name}: storage returned {status}")
                    size = 0
                    with target.open("wb") as out:
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > limit:
                                raise PacdsError(
                                    422, "log_too_large", f"log {name}: larger than {self._config.max_file_size_mb} MB"
                                )
                            out.write(chunk)
        except PacdsError:
            target.unlink(missing_ok=True)
            raise
        except httpx.HTTPError:
            target.unlink(missing_ok=True)
            raise PacdsError(502, "log_unreachable", f"log {name}: storage is unreachable") from None
