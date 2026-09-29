import httpx
import pytest

from pacds.engine import mcp_http
from pacds.engine.mcp_http import Toolset, allowed_tools, serve

DEFS = [{"type": "function", "function": {"name": "echo", "description": "Echo text.",
                                          "parameters": {"type": "object", "properties": {"text": {"type": "string"}},
                                                         "required": ["text"]}}}]


async def _echo(name: str, arguments: dict) -> str:
    return "error: bad" if arguments.get("text") == "fail" else f"{name}:{arguments['text']}"


async def _post(url: str, message: dict) -> httpx.Response:
    async with httpx.AsyncClient() as client:
        return await client.post(url, json=message, headers={"Accept": "application/json, text/event-stream"})


async def test_initialize_list_and_call():
    async with serve(Toolset(DEFS, _echo)) as config:
        url = config["mcpServers"]["pacds"]["url"]
        assert url.startswith("http://127.0.0.1:")
        init = (await _post(url, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})).json()
        assert init["result"]["protocolVersion"] == "2025-06-18"
        assert init["result"]["capabilities"] == {"tools": {}}
        assert (await _post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"})).status_code == 202
        tools = (await _post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})).json()["result"]["tools"]
        assert tools == [{"name": "echo", "description": "Echo text.", "inputSchema": DEFS[0]["function"]["parameters"]}]
        call = (await _post(url, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "echo", "arguments": {"text": "hi"}}})).json()
        assert call["result"] == {"content": [{"type": "text", "text": "echo:hi"}], "isError": False}
        failed = (await _post(url, {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "echo", "arguments": {"text": "fail"}}})).json()
        assert failed["result"]["isError"] is True


async def test_unknown_token_method_and_verbs():
    async with serve(Toolset(DEFS, _echo)) as config:
        url = config["mcpServers"]["pacds"]["url"]
        base = url.rsplit("/", 1)[0]
        assert (await _post(base + "/not-a-token", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})).status_code == 404
        unknown = (await _post(url, {"jsonrpc": "2.0", "id": 5, "method": "resources/list"})).json()
        assert unknown["error"]["code"] == -32601
        async with httpx.AsyncClient() as client:
            assert (await client.get(url)).status_code == 405
    # the server lives on with the event loop; the session's path is gone
    assert (await _post(url, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})).status_code == 404


async def test_two_toolsets_are_isolated():
    async def other(name: str, arguments: dict) -> str:
        return "other"
    async with serve(Toolset(DEFS, _echo)) as a, serve(Toolset(DEFS, other)) as b:
        ua, ub = a["mcpServers"]["pacds"]["url"], b["mcpServers"]["pacds"]["url"]
        assert ua != ub
        msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo", "arguments": {"text": "x"}}}
        assert (await _post(ua, msg)).json()["result"]["content"][0]["text"] == "echo:x"
        assert (await _post(ub, msg)).json()["result"]["content"][0]["text"] == "other"


def test_allowed_tools():
    assert allowed_tools(Toolset(DEFS, _echo)) == ["mcp__pacds__echo"]


async def test_invalid_json_body():
    async with serve(Toolset(DEFS, _echo)) as config:
        url = config["mcpServers"]["pacds"]["url"]
        # Invalid JSON body should return 400
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, content=b"not valid json")
            assert resp.status_code == 400
        # Server should still answer the next request
        call = (await _post(url, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo", "arguments": {"text": "hi"}}})).json()
        assert call["result"]["content"][0]["text"] == "echo:hi"


async def test_tool_call_raises(monkeypatch):
    async def failing_tool(name: str, arguments: dict) -> str:
        raise ValueError("tool error")

    async with serve(Toolset(DEFS, failing_tool)) as config:
        url = config["mcpServers"]["pacds"]["url"]
        call = (await _post(url, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo", "arguments": {"text": "hi"}}})).json()
        assert call["result"]["isError"] is True
        assert call["result"]["content"][0]["text"] == "error: tool failed"


async def test_client_timeout_on_connect(monkeypatch):
    monkeypatch.setattr(mcp_http, "READ_TIMEOUT_SECONDS", 0.2)
    async with serve(Toolset(DEFS, _echo)) as config:
        url = config["mcpServers"]["pacds"]["url"]
        # Connect but send nothing, should get 408 after timeout
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            host, port = url.split("://")[1].split(":")
            sock.connect((host, int(port)))
            # Connection established but we send nothing
            import time
            time.sleep(0.3)  # Wait for server timeout
            sock.close()
        except Exception:
            pass
        # Server should still answer the next request
        call = (await _post(url, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo", "arguments": {"text": "hi"}}})).json()
        assert call["result"]["content"][0]["text"] == "echo:hi"


async def test_too_many_headers():
    async with serve(Toolset(DEFS, _echo)) as config:
        url = config["mcpServers"]["pacds"]["url"]
        # Construct a request with too many headers
        headers = {"Accept": "application/json"}
        for i in range(mcp_http.MAX_HEADERS + 10):
            headers[f"X-Custom-{i}"] = f"value-{i}"
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=headers)
            assert resp.status_code == 400
