# meiva:link services/desktop/browser/docs/operations.md

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Awaitable, Callable
from contextlib import ExitStack
from pathlib import Path

from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, ImageContent, TextContent

from services.desktop.browser.mcp_upstream import UpstreamCallTimeoutError

from .remote_files import (
    MAX_FILE_BYTES,
    Artifact,
    decode_bytes,
    encode_bytes,
    file_argument,
    replace_paths,
    stage_upload,
)
from .remote_pages import require_remote_tool

_UPLOAD_RETENTION_S = 600
_MAX_UPLOADS = 32
_IMAGE_SUFFIXES = {"image/png": ".png", "image/jpeg": ".jpeg", "image/webp": ".webp"}

type InvokeTool = Callable[[str, dict[str, object], int], Awaitable[CallToolResult]]


def remote_result(
    tool: str,
    args: dict[str, object],
    result: CallToolResult,
) -> dict[str, object]:
    files: dict[str, list[Artifact]] = {}
    if "filePath" in args and tool != "upload_file" and not result.is_error:
        if tool == "take_screenshot":
            image = next((item for item in result.content if isinstance(item, ImageContent)), None)
            if image is None or image.mime_type not in _IMAGE_SUFFIXES:
                raise ValueError("Remote Chrome screenshot did not return a bounded image")
            data = decode_bytes(image.data)
            suffix = _IMAGE_SUFFIXES[image.mime_type]
        else:
            data = "\n".join(
                item.text for item in result.content if isinstance(item, TextContent)
            ).encode()
            suffix = ".txt"
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("Chrome output exceeded the file transfer limit")
        source = "output" + suffix
        files["filePath"] = [{"path": source, "data": encode_bytes(data)}]
        result = CallToolResult(
            content=[TextContent(type="text", text=f"Saved Chrome output to {source}")]
        )
    return {"mcp": result.model_dump(mode="json", by_alias=True), "files": files}


class BrowserStaging:
    def __init__(self, root: Path, call: InvokeTool) -> None:
        root.mkdir(mode=0o700, exist_ok=True)
        if root.is_symlink():
            raise ValueError("Chrome staging directory must be local")
        root.chmod(0o700)
        for stale in root.glob("chrome-*"):
            if stale.is_dir() and not stale.is_symlink():
                shutil.rmtree(stale)
        self._root = root
        self._call = call
        self._uploads: dict[asyncio.TimerHandle, ExitStack] = {}
        self._pending = 0
        self._closed = False

    def close(self) -> None:
        self._closed = True
        for timer in tuple(self._uploads):
            self._release(timer)

    def _release(self, timer: asyncio.TimerHandle) -> None:
        timer.cancel()
        self._uploads.pop(timer).close()

    def _retain(self, stack: ExitStack) -> None:
        if self._closed:
            return
        loop = asyncio.get_running_loop()
        timer = loop.call_later(_UPLOAD_RETENTION_S, lambda: self._release(timer))
        self._uploads[timer] = stack.pop_all()

    async def run(
        self,
        tool: str,
        args: dict[str, object],
        inputs: dict[str, object],
        agent_id: int,
    ) -> dict[str, object]:
        if self._closed:
            raise RuntimeError("Remote Chrome staging is closed")
        require_remote_tool(tool, args)
        _ = file_argument(tool, args)
        if tool != "upload_file":
            if inputs:
                raise ValueError("Unexpected Chrome upload data")
            safe_args = {key: value for key, value in args.items() if key != "filePath"}
            return remote_result(tool, args, await self._call(tool, safe_args, agent_id))
        return await self._upload(tool, args, inputs, agent_id)

    async def _upload(
        self,
        tool: str,
        args: dict[str, object],
        inputs: dict[str, object],
        agent_id: int,
    ) -> dict[str, object]:
        if len(self._uploads) + self._pending >= _MAX_UPLOADS:
            raise ValueError("Remote Chrome upload staging is full")
        self._pending += 1
        try:
            with ExitStack() as stack:
                staged = stack.enter_context(stage_upload(args, inputs, parent=self._root))
                try:
                    result = await self._call(tool, staged, agent_id)
                except (TimeoutError, UpstreamCallTimeoutError, MCPError, asyncio.CancelledError):
                    self._retain(stack)
                    raise
                if not result.is_error:
                    self._retain(stack)
                result = replace_paths(result, {str(staged["filePath"]): str(args["filePath"])})
                return remote_result(tool, args, result)
        finally:
            self._pending -= 1
