#!/usr/bin/env python3
"""Fake `claude` for unit tests: records its argv and stdin, then replays a canned stream.

FAKE_CLAUDE_STREAM names a file in tests/unit/claude_streams (or a comma list: one per invocation, in order, tracked
in FAKE_CLAUDE_LOG's line count). FAKE_CLAUDE_LOG gets one JSON line per invocation: {"argv", "stdin", "cwd", "pid", "env" (sorted variable names),
"mcp_mode" (the --mcp-config file's permission bits, when it is a file)}.
FAKE_CLAUDE_CALL_TOOL=name:json makes the fake call that MCP tool through --mcp-config (when given) before replaying,
and put the tool's text into the replayed tool_result of id t1. "auth.jsonl" exits 1 with a login error; "limit_stderr"
exits 1 with a usage-limit message on stderr and no result; "hang" sleeps forever.
Without --no-session-persistence it writes a transcript under $HOME/.claude/projects/<cwd with - for non-alphanumerics>/,
as the CLI does.
"""

import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

STREAMS = Path(__file__).parent / "claude_streams"


def main() -> None:
    argv = sys.argv[1:]
    if os.environ.get("FAKE_CLAUDE_STREAM") == "noread":
        sys.exit(1)  # exits without reading stdin
    stdin = sys.stdin.read()
    log = Path(os.environ["FAKE_CLAUDE_LOG"])
    previous = log.read_text().count("\n") if log.exists() else 0
    mcp_mode = None
    if "--mcp-config" in argv and os.path.isfile(argv[argv.index("--mcp-config") + 1]):
        mcp_mode = oct(os.stat(argv[argv.index("--mcp-config") + 1]).st_mode & 0o777)
    with log.open("a") as out:
        out.write(json.dumps({"argv": argv, "stdin": stdin, "cwd": os.getcwd(), "pid": os.getpid(), "env": sorted(os.environ),
                              "mcp_mode": mcp_mode}) + "\n")
    streams = os.environ["FAKE_CLAUDE_STREAM"].split(",")
    stream = streams[min(previous, len(streams) - 1)]
    if "--no-session-persistence" not in argv:
        transcripts = Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", os.getcwd())
        transcripts.mkdir(parents=True, exist_ok=True)
        (transcripts / f"session-{previous}.jsonl").write_text(stdin)
    if stream == "hang":
        time.sleep(3600)
    if stream == "limit_stderr":
        print("Claude AI usage limit reached|1790000000", file=sys.stderr)
        sys.exit(1)
    if stream == "auth.jsonl":
        print("Invalid API key · Please run /login", file=sys.stderr)
        sys.exit(1)
    tool_text = None
    if os.environ.get("FAKE_CLAUDE_CALL_TOOL") and "--mcp-config" in argv:
        name, _, raw = os.environ["FAKE_CLAUDE_CALL_TOOL"].partition(":")
        value = argv[argv.index("--mcp-config") + 1]
        config = json.loads(Path(value).read_text() if os.path.isfile(value) else value)
        url = config["mcpServers"]["pacds"]["url"]
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": json.loads(raw)}}).encode()
        request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        tool_text = json.loads(urllib.request.urlopen(request).read())["result"]["content"][0]["text"]
    for line in (STREAMS / stream).read_text().splitlines():
        event = json.loads(line)
        if tool_text is not None and event.get("type") == "user":
            for block in event["message"]["content"]:
                if block.get("tool_use_id") == "t1":
                    block["content"] = tool_text
        print(json.dumps(event), flush=True)


if __name__ == "__main__":
    main()
