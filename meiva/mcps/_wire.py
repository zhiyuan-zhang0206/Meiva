# meiva:note
# The JSON-line protocol the MCP daemon speaks, as value builders: one response dict per request,
# and the bytes of one line. The functions take the MCP SDK's own `Tool` and `CallToolResult` and
# return plain dicts. Content keeps the camelCase wire contract agents parse (`isError`,
# `structuredContent`) while the SDK's own fields are snake_case.
# meiva:link meiva/mcps/docs/mcps.meiva.okf.md
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping

from mcp.types import CallToolResult, Tool


# meiva:note
# The reply to the watchdog's liveness probe; it opens no session, so a slow MCP server can never make a busy daemon look dead.
def ping_response(req_id: object) -> dict[str, object]:
    return {"id": req_id, "ok": True, "result": "pong"}


def error_response(req_id: object, error: str) -> dict[str, object]:
    return {"id": req_id, "ok": False, "error": error}


def tools_response(req_id: object, tools: Iterable[Tool]) -> dict[str, object]:
    wire = [
        {
            "name": tool.name,
            "description": tool.description or "",
            "input_schema": tool.input_schema or {},
        }
        for tool in tools
    ]
    return {"id": req_id, "ok": True, "result": wire}


# meiva:note
# A tool result in the daemon's wire shape. A content block that is not a pydantic model fails fast: the SDK contract says every `ContentBlock` is one, so a block without `model_dump` means the SDK changed or a new type arrived, and turning it into an `unknown` block would let the agent mis-parse it as legal data. `isError` rides along as data; judging it is the client's job.
def call_response(req_id: object, result: CallToolResult) -> dict[str, object]:
    content: list[dict[str, object]] = []
    for block in result.content:
        if not hasattr(block, "model_dump"):
            raise TypeError(
                f"Unrecognized MCP content block type {type(block).__name__!r} "
                f"({block!r}); ContentBlock should be a pydantic model — SDK upgrade or new type?"
            )
        dumped = block.model_dump(mode="python", exclude_none=True, by_alias=True)
        content.append(dumped)
    return {
        "id": req_id,
        "ok": True,
        "result": {
            "content": content,
            "isError": result.is_error,
            "structuredContent": result.structured_content,
        },
    }


def encode_line(response: Mapping[str, object]) -> bytes:
    return (json.dumps(response, ensure_ascii=False) + "\n").encode()
