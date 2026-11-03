"""Wire types for the computer-mcp line protocol (one JSON object per line).

Mirrors `services/desktop/browser/protocol.py`: the per-agent bridges and the MCP
daemon's direct dial speak this over the `computer-mcp` Unix socket; the daemon
serves it. `agent_id` rides on every request so the daemon can gate, quota, and
audit per agent — the bridge stamps it from the calling agent's identity.

  Request:  {"id": 1, "method": "ping"}
            {"id": 2, "method": "call_tool", "tool": "click", "args": {...}, "agent_id": 42}
  Response: {"id": 1, "ok": true,  "result": "pong"}
            {"id": 2, "ok": true,  "result": {"content": [{"type": "text", "text": "..."}], "isError": false}}
            {"id": 2, "ok": false, "error": "message"}

A call_tool result is an MCP CallToolResult dump: the tool's plain dict rides
as one JSON text block, the contract both the per-agent wrapper and the direct
dial validate against.
"""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict


class Request(TypedDict):
    id: int
    method: str
    tool: NotRequired[str | None]  # call_tool only
    args: NotRequired[dict[str, object] | None]  # call_tool only
    agent_id: NotRequired[int | None]  # the calling agent's identity, stamped by the bridge


# meiva:note
# A request as the daemon reads it off the wire: every member is optional and still `object`,
# because a client is not trusted to send the shape `Request` declares. The daemon narrows each
# member it uses and answers a malformed one with an error instead of failing the connection.
class WireRequest(TypedDict, total=False):
    id: object
    method: object
    tool: object
    args: object
    agent_id: object


class OkResponse(TypedDict):
    id: int | None  # echoes the request id; None when the request never parsed
    ok: Literal[True]
    result: (
        dict[str, object] | list[dict[str, object]] | str | None
    )  # tool dicts / tool result / "pong"


class ErrResponse(TypedDict):
    id: int | None  # echoes the request id; None when the request never parsed
    ok: Literal[False]
    error: str


Response = OkResponse | ErrResponse
