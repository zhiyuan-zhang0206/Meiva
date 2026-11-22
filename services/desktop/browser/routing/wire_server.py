# meiva:link services/desktop/browser/docs/operations.md

from __future__ import annotations

import asyncio
import json
from typing import Protocol

from mcp.types import CallToolResult

from base.host.boundary.json_values import is_object_dict
from services.desktop.browser import page_lifecycle
from services.desktop.browser.mcp_upstream import UPSTREAM_DOWN_MSG
from services.desktop.browser.page_lifecycle import PageDaemon, handle_release_agent_page
from services.desktop.browser.protocol import WIRE_REQUEST, Response, WireRequest

from .remote_pages import REMOTE_TOOLS
from .staging import BrowserStaging


class WireBrowser(PageDaemon, Protocol):
    dead: asyncio.Event
    staging: BrowserStaging | None

    async def list_tools(self) -> list[dict[str, object]]: ...

    async def call_tool_for_agent(
        self, name: str, args: dict[str, object], agent_id: int, *, remote: bool = False
    ) -> CallToolResult: ...


async def _call_tool_response(
    daemon: WireBrowser, req: WireRequest, req_id: int | None, conn_page: int | None
) -> tuple[Response, int | None]:
    tool = req.get("tool")
    if not isinstance(tool, str) or not tool:
        error = "call_tool requires a non-empty 'tool' name"
        return {"id": req_id, "ok": False, "error": error}, conn_page
    raw_args = req.get("args")
    if raw_args is not None and not is_object_dict(raw_args):
        return {"id": req_id, "ok": False, "error": "args must be an object"}, conn_page
    args = raw_args or {}
    agent_id = req.get("agent_id")
    remote = req.get("method") == "call_remote_tool"
    if remote:
        inputs = req.get("inputs", {})
        if type(agent_id) is not int or not is_object_dict(inputs) or daemon.staging is None:
            raise ValueError("Remote Chrome requires an agent identity and valid staging data")
        result = await daemon.staging.run(tool, args, inputs, agent_id)
        return {"id": req_id, "ok": True, "result": result}, conn_page
    if isinstance(agent_id, int) and not isinstance(agent_id, bool):
        result = await daemon.call_tool_for_agent(tool, args, agent_id)
    else:
        result, conn_page = await page_lifecycle.forward_legacy_call(daemon, tool, args, conn_page)
    return {"id": req_id, "ok": True, "result": result.model_dump(mode="json")}, conn_page


async def _respond(
    daemon: WireBrowser,
    req: WireRequest,
    req_id: int | None,
    method: str | None,
    conn_page: int | None,
) -> tuple[Response, int | None]:
    try:
        if method in {"list_tools", "list_remote_tools"}:
            tools = await daemon.list_tools()
            if method == "list_remote_tools":
                tools = [tool for tool in tools if tool.get("name") in REMOTE_TOOLS]
            return {"id": req_id, "ok": True, "result": tools}, conn_page
        if method in {"call_tool", "call_remote_tool"}:
            return await _call_tool_response(daemon, req, req_id, conn_page)
        if method == "release_agent_page":
            return await handle_release_agent_page(daemon, req, req_id), conn_page
        return {"id": req_id, "ok": False, "error": f"Unknown method: {method}"}, conn_page
    except Exception as e:
        return {"id": req_id, "ok": False, "error": f"{type(e).__name__}: {e}"}, conn_page


async def serve_line(
    line: bytes,
    writer: asyncio.StreamWriter,
    daemon: WireBrowser | None,
    conn_page: int | None,
) -> int | None:
    try:
        req = WIRE_REQUEST.validate_json(line)
    except ValueError as e:
        _write(writer, {"id": None, "ok": False, "error": f"JSON parse error: {e}"})
        await writer.drain()
        return conn_page

    raw_id = req.get("id")
    req_id = raw_id if isinstance(raw_id, int) and not isinstance(raw_id, bool) else None
    raw_method = req.get("method")
    method = raw_method if isinstance(raw_method, str) else None

    if method in {"ping", "status"}:
        ready = daemon is not None and not daemon.dead.is_set()
        _write(writer, {"id": req_id, "ok": True, "result": ready if method == "status" else None})
        await writer.drain()
        return conn_page

    if daemon is None or daemon.dead.is_set():
        _write(writer, {"id": req_id, "ok": False, "error": UPSTREAM_DOWN_MSG})
        await writer.drain()
        return conn_page

    resp, conn_page = await _respond(daemon, req, req_id, method, conn_page)
    _write(writer, resp)
    await writer.drain()
    return conn_page


def _write(writer: asyncio.StreamWriter, obj: Response) -> None:
    writer.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())
