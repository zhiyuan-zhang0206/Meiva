# meiva:note
# `meiva computer`: operator commands for the computer-use daemon.
#
# Today one verb, `release`: the operator's last resort to force-release the screen when a
# session is wedged (a holder that stopped acting but whose lease has not expired yet). The next
# FIFO waiter takes over immediately. This talks the daemon's line protocol directly (no agent
# identity: it is an operator action, so the daemon logs it but does not audit it as a
# computer_action).

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

from pydantic import JsonValue

from base.paths import computer_mcp_socket


# meiva:note
# Force-release the screen from its current holder. `home` is the one the invocation settled
# on; the daemon's socket lives under it.
def cmd_computer_release(*, home: Path) -> int:
    sock_path = str(computer_mcp_socket(home))
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(sock_path)
    except (FileNotFoundError, ConnectionRefusedError) as e:
        print(f"computer-mcp daemon not reachable at {sock_path}: {e}", file=sys.stderr)
        return 1
    with s:
        stream = s.makefile("rwb")
        req = {
            "id": 1,
            "method": "call_tool",
            "tool": "release_control",
            "args": {"force": True},
        }
        stream.write((json.dumps(req) + "\n").encode())
        stream.flush()
        line = stream.readline()
    if not line:
        print("computer-mcp daemon closed the connection", file=sys.stderr)
        return 1
    try:
        resp: JsonValue = json.loads(line)
    except json.JSONDecodeError:
        resp = None
    if not isinstance(resp, dict):
        print("computer-mcp daemon returned a corrupted response", file=sys.stderr)
        return 1
    if resp.get("ok") is not True:
        print(f"release failed: {resp.get('error', 'unknown error')}", file=sys.stderr)
        return 1
    result: JsonValue = None
    match resp.get("result"):
        case {"content": [{"text": str(text)}, *_]}:
            try:
                result = json.loads(text)
            except json.JSONDecodeError:
                result = None
        case _:
            result = None
    match result:
        case {"released": None}:
            print("screen was free (no holder)")
            return 0
        case {"released": int(released)} if not isinstance(released, bool):
            print(f"released the screen from agent {released}")
            return 0
        case _:
            print("computer-mcp daemon returned an unexpected release payload", file=sys.stderr)
            return 1
