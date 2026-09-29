"""A minimal MCP server over streamable HTTP on localhost, so a `claude -p` session calls tools that run in this process.

It implements what Claude Code needs of the MCP specification (2025-06-18): initialize, ping, tools/list and
tools/call as POSTed JSON-RPC answered with JSON; notifications get 202; GET and DELETE get 405 (no server-initiated
stream, no sessions). Each session registers a Toolset under an unguessable path and unregisters it when done, so a
session reaches only its own tools. Written against the specification, not the `mcp` SDK, to keep a small surface.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

SERVER_NAME = "pacds"
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class Toolset:
    """Chat-completions function definitions and the coroutine that runs one call by name."""

    definitions: list[dict[str, Any]]
    call: Callable[[str, dict[str, Any]], Awaitable[str]]


def allowed_tools(toolset: Toolset) -> list[str]:
    return [f"mcp__{SERVER_NAME}__{d['function']['name']}" for d in toolset.definitions]


class _Server:
    def __init__(self) -> None:
        self._toolsets: dict[str, Toolset] = {}
        self._server: asyncio.Server | None = None
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    def register(self, toolset: Toolset) -> str:
        token = secrets.token_urlsafe(24)
        self._toolsets[token] = toolset
        return token

    def unregister(self, token: str) -> None:
        self._toolsets.pop(token, None)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            status, body = await self._respond(reader)
        except Exception:  # noqa: BLE001 - a malformed request must not stop the server
            logger.exception("mcp request failed")
            status, body = 400, None
        payload = b"" if body is None else json.dumps(body).encode()
        head = [f"HTTP/1.1 {status} {_REASONS.get(status, 'Error')}", f"Content-Length: {len(payload)}", "Connection: close"]
        if body is not None:
            head.append("Content-Type: application/json")
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode() + payload)
        try:
            await writer.drain()
        finally:
            writer.close()

    async def _respond(self, reader: asyncio.StreamReader) -> tuple[int, Any]:
        request_line = (await reader.readline()).decode("latin-1").split()
        headers: dict[str, str] = {}
        while (line := (await reader.readline()).decode("latin-1").strip()):
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        if len(request_line) < 2:
            return 400, None
        method, path = request_line[0], request_line[1]
        toolset = self._toolsets.get(path.removeprefix("/mcp/")) if path.startswith("/mcp/") else None
        if toolset is None:
            return 404, None
        if method != "POST":
            return 405, None
        length = int(headers.get("content-length") or 0)
        if length > MAX_BODY_BYTES:
            return 413, None
        message = json.loads(await reader.readexactly(length))
        reply = await _dispatch(toolset, message)
        return (202, None) if reply is None else (200, reply)


_REASONS = {200: "OK", 202: "Accepted", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed", 413: "Payload Too Large"}


async def _dispatch(toolset: Toolset, message: dict[str, Any]) -> dict[str, Any] | None:
    if "id" not in message:  # a notification
        return None
    method, params = message.get("method"), message.get("params") or {}
    if method == "initialize":
        requested = params.get("protocolVersion")
        result: dict[str, Any] = {
            "protocolVersion": requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": "1"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": d["function"]["name"], "description": d["function"].get("description", ""),
                             "inputSchema": d["function"]["parameters"]} for d in toolset.definitions]}
    elif method == "tools/call":
        arguments = params.get("arguments")
        text = await toolset.call(params.get("name", ""), arguments if isinstance(arguments, dict) else {})
        result = {"content": [{"type": "text", "text": text}], "isError": text.startswith("error:")}
    else:
        return {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": message["id"], "result": result}


_servers: dict[asyncio.AbstractEventLoop, _Server] = {}


async def _server() -> _Server:
    loop = asyncio.get_running_loop()
    for stale in [other for other in _servers if other.is_closed()]:
        del _servers[stale]
    if loop not in _servers:
        server = _Server()
        await server.start()
        _servers[loop] = server
    return _servers[loop]


@asynccontextmanager
async def serve(toolset: Toolset) -> AsyncIterator[dict[str, Any]]:
    """Register `toolset` for one session; yields the --mcp-config object that reaches it."""
    server = await _server()
    token = server.register(toolset)
    try:
        yield {"mcpServers": {SERVER_NAME: {"type": "http", "url": f"http://127.0.0.1:{server.port}/mcp/{token}"}}}
    finally:
        server.unregister(token)
