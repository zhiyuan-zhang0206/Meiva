# meiva:link meiva_builtins/mcps/docs/chrome/chrome.md

from __future__ import annotations

import asyncio
import json
from contextlib import ExitStack
from pathlib import Path

from mcp.types import CallToolResult

from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of
from services.desktop.browser.routing.remote_files import replace_paths, stage_files
from services.desktop.browser.routing.remote_rpc import call_browser_socket

from .in_flight import RequestTokens, admitted

UPLOAD_RETENTION_S = 600
_MAX_UPLOADS = 32


class BrowserRpc:
    def __init__(
        self,
        sock: Path,
        *,
        dispatch_sem: asyncio.Semaphore,
        requests: RequestTokens,
    ) -> None:
        self._socket = sock
        self._uploads: dict[asyncio.TimerHandle, ExitStack] = {}
        self._dispatch_sem = dispatch_sem
        self._requests = requests

    def close(self) -> None:
        for timer in tuple(self._uploads):
            self._release(timer)

    def _release(self, timer: asyncio.TimerHandle) -> None:
        timer.cancel()
        self._uploads.pop(timer).close()

    def _retain(self, stack: ExitStack) -> asyncio.TimerHandle:
        def release() -> None:
            self._release(timer)

        timer = asyncio.get_running_loop().call_later(UPLOAD_RETENTION_S, release)
        self._uploads[timer] = stack.pop_all()
        return timer

    async def _tool(self, req: dict[str, object]) -> dict[str, object]:
        tool, args, agent_id = req.get("tool"), req.get("args"), req.get("agent_id")
        inputs = req.get("inputs", {})
        if not isinstance(tool, str) or not tool or not is_object_dict(args):
            raise ValueError("Browser RPC requires a tool name and object arguments")
        if not isinstance(agent_id, int) or isinstance(agent_id, bool) or agent_id <= 0:
            raise ValueError("Remote Chrome requires a positive agent identity")
        if not is_object_dict(inputs) or "host" in args:
            raise ValueError("Invalid remote browser file data or nested host")
        return await self._forward(tool, args, inputs, agent_id)

    async def _forward(
        self, tool: str, args: dict[str, object], inputs: dict[str, object], agent_id: int
    ) -> dict[str, object]:
        if inputs and len(self._uploads) >= _MAX_UPLOADS:
            raise ValueError("Remote Chrome upload staging is full. Retry after it expires.")
        with ExitStack() as scratch:
            staged = scratch.enter_context(
                stage_files(tool, args, inputs, parent=self._socket.parent)
            )
            retention = self._retain(scratch) if inputs else None
            try:
                raw = await call_browser_socket(
                    self._socket,
                    {
                        "method": "call_remote_tool",
                        "tool": tool,
                        "args": staged.args,
                        "agent_id": agent_id,
                    },
                )
            except RuntimeError:
                if retention is not None and retention in self._uploads:
                    self._release(retention)
                raise
            result = CallToolResult.model_validate(raw)
            if inputs:
                if result.is_error and retention is not None and retention in self._uploads:
                    self._release(retention)
                result = replace_paths(
                    result, {str(staged.args["filePath"]): str(args["filePath"])}
                )
            return {
                "mcp": result.model_dump(mode="json", by_alias=True),
                "files": {} if result.is_error else staged.outputs(),
            }

    async def _dispatch(self, req: dict[str, object]) -> object:
        method = req.get("method")
        if method == "status":
            try:
                return (
                    await call_browser_socket(self._socket, {"method": "status"}, timeout_s=1.0)
                    is True
                )
            except (OSError, RuntimeError, ValueError):
                return False
        if method == "list_tools":
            return await call_browser_socket(self._socket, {"method": "list_tools"}, timeout_s=2.0)
        if method == "call_tool":
            return await self._tool(req)
        raise ValueError("Unknown browser RPC method")

    async def route(self, body: bytes) -> tuple[int, bytes, str]:
        try:
            req = object_of(json.loads, body)
            if not is_object_dict(req):
                req = {}
            with admitted(requests=self._requests):
                async with self._dispatch_sem:
                    result = await self._dispatch(req)
            response: dict[str, object] = {"ok": True, "result": result}
        except (OSError, RuntimeError, ValueError) as exc:
            response = {"ok": False, "error": str(exc)}
        return 200, json.dumps(response, ensure_ascii=False).encode(), "application/json"
