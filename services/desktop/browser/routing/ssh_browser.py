# meiva:link meiva_builtins/mcps/docs/chrome/chrome.md

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, suppress
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from mcp import ClientSession
from mcp.types import CallToolResult, ListToolsResult, Tool

from base.clock import Timers
from base.host.remote_hosts import RemoteHostConfig
from services.desktop.browser.mcp_upstream import create_upstream
from services.desktop.remote.ssh_tunnel import SshTunnelManager

from .cdp_screenshot import capture_owned_viewport
from .remote_files import CallerFiles, replace_paths
from .remote_pages import (
    PAGE_LIST_TOOLS,
    PAGE_SCOPED_TOOLS,
    ListedPage,
    authorize_page,
    page_number,
    reconnected,
    scoped_structured_page_list,
    structured_fields,
    structured_pages,
)
from .remote_rpc import RPC_TIMEOUT_S
from .ssh_upload import stage_ssh_upload
from .staging import remote_result


class BrowserToolSession(Protocol):
    async def call_tool(self, name: str, arguments: dict[str, object]) -> CallToolResult: ...


type BrowserAction = Callable[[ClientSession, list[AsyncExitStack]], Awaitable[object]]


class SshBrowser:
    def __init__(
        self,
        config: RemoteHostConfig,
        *,
        timers: Timers,
        tasks: asyncio.TaskGroup,
        connect_timeout_s: float,
        stage_upload: Callable[
            [RemoteHostConfig, str, str], AbstractAsyncContextManager[str]
        ] = stage_ssh_upload,
    ) -> None:
        self.config = config
        self._timers = timers
        self._tasks = tasks
        self._timeout_s = connect_timeout_s
        self._stage_upload = stage_upload
        self._queue: asyncio.Queue[tuple[BrowserAction, asyncio.Future[object]]] = asyncio.Queue()
        self._task: asyncio.Task[Exception | None] | None = None
        self._pages: set[int] = set()
        self._current: int | None = None
        self._cdp_port: int | None = None

    async def _run(self, ready: asyncio.Future[None]) -> Exception | None:
        tunnel = SshTunnelManager(timers=self._timers)
        try:
            endpoint = await asyncio.to_thread(tunnel.endpoint_for, self.config, browser=True)
            self._cdp_port = urlsplit(endpoint).port
            session, upstream = await create_upstream(
                endpoint,
                asyncio.Event(),
                connect_timeout_reader=lambda: self._timeout_s,
                explicit_pages=True,
                interop=True,
            )
            async with upstream:
                uploads: list[AsyncExitStack] = []
                try:
                    if ready.cancelled():
                        return None
                    ready.set_result(None)
                    await self._serve(session, uploads)
                finally:
                    await self._finish(session, uploads)
        except Exception as exc:
            if not ready.done():
                ready.set_exception(exc)
            return exc
        finally:
            await asyncio.to_thread(tunnel.close_all)

    async def _serve(self, session: ClientSession, uploads: list[AsyncExitStack]) -> None:
        while True:
            action, reply = await self._queue.get()
            try:
                value = await action(session, uploads)
            except Exception as exc:
                if not reply.done():
                    reply.set_exception(exc)
            else:
                if not reply.done():
                    reply.set_result(value)

    async def _finish(self, session: ClientSession, uploads: list[AsyncExitStack]) -> None:
        async with asyncio.TaskGroup() as cleanup:
            for upload in uploads:
                _ = cleanup.create_task(upload.aclose())
            _ = cleanup.create_task(self._close_tabs(session))

    async def _close_tabs(self, session: ClientSession) -> None:
        if not self._pages:
            return
        with suppress(RuntimeError, OSError, ValueError):
            async with asyncio.timeout(2.0):
                _ = await self._refresh_pages(session)
                for page in tuple(self._pages):
                    result = await session.call_tool("close_page", {"pageId": page})
                    self._require_generation(result)

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                _ = await task
        self._pages.clear()
        self._current = None

    async def _request(self, action: BrowserAction) -> object:
        try:
            if self._task is not None and self._task.done():
                await self.close()
            if self._task is None:
                ready = asyncio.get_running_loop().create_future()
                self._task = self._tasks.create_task(self._run(ready))
                await ready
            return await self._roundtrip(self._task, action)
        except ValueError:
            if self._task is not None and self._task.done():
                await self.close()
            raise
        except BaseException:
            await self.close()
            raise

    async def _roundtrip(
        self, task: asyncio.Task[Exception | None], action: BrowserAction
    ) -> object:
        reply: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        self._queue.put_nowait((action, reply))
        try:
            done, _ = await asyncio.wait(
                {reply, task}, timeout=RPC_TIMEOUT_S, return_when=asyncio.FIRST_COMPLETED
            )
            if reply in done:
                return reply.result()
            if task in done:
                raise ConnectionError(
                    "SSH Chrome upstream closed during the request"
                ) from task.result()
            raise TimeoutError("SSH Chrome tool result is unknown after its request deadline")
        finally:
            reply.cancel()

    async def list_tools(self) -> list[Tool]:
        async def list_tools(session: ClientSession, _uploads: list[AsyncExitStack]) -> object:
            return await session.list_tools()

        return ListToolsResult.model_validate(await self._request(list_tools)).tools

    async def _paged_call(
        self,
        session: BrowserToolSession,
        name: str,
        args: dict[str, object],
    ) -> CallToolResult:
        if name == "new_page" and "isolatedContext" in args:
            raise ValueError("SSH Chrome does not support named isolated contexts")
        previous = await self._refresh_pages(session)
        authorize_page(name, args, self._pages.__contains__)
        if name == "list_pages":
            return scoped_structured_page_list(previous, self._pages.__contains__, self._current)
        if name in PAGE_SCOPED_TOOLS:
            page = page_number(args.get("pageId")) if "pageId" in args else self._current
            if page is None:
                raise ValueError("No owned SSH Chrome page selected. Call new_page first.")
            args = {**args, "pageId": page}
            if name == "take_screenshot":
                return await self._screenshot(session, page, args)
        if name == "new_page":
            args = {"background": True, **args}
        result = await session.call_tool(name, args)
        self._require_generation(result)
        if result.is_error:
            if name in PAGE_SCOPED_TOOLS:
                _ = await self._refresh_pages(session)
            return result
        if name in PAGE_LIST_TOOLS:
            pages = self._read_pages(result)
            self._remember_page(name, args, pages, previous)
            return scoped_structured_page_list(pages, self._pages.__contains__, self._current)
        return result

    async def _screenshot(
        self,
        session: BrowserToolSession,
        page: int,
        args: Mapping[str, object],
    ) -> CallToolResult:
        tab = await session.call_tool("get_tab_id", {"pageId": page})
        self._require_generation(tab)
        tab_id = structured_fields(tab).get("tabId")
        if tab.is_error or not isinstance(tab_id, str) or self._cdp_port is None:
            _ = await self._refresh_pages(session)
            raise ValueError("Could not verify the owned SSH Chrome target")
        return await capture_owned_viewport(self._cdp_port, tab_id, args, tab_target=True)

    def _require_generation(self, result: CallToolResult) -> None:
        if reconnected(result):
            self._pages.clear()
            self._current = None
            raise ValueError("SSH Chrome reconnected. Open a new page before calling tools.")

    def _read_pages(self, result: CallToolResult) -> dict[int, ListedPage]:
        try:
            pages = structured_pages(result)
        except ValueError:
            self._pages.clear()
            self._current = None
            raise
        self._pages.intersection_update(pages)
        if self._current not in self._pages:
            self._current = None
        return pages

    async def _refresh_pages(self, session: BrowserToolSession) -> dict[int, ListedPage]:
        listing = await session.call_tool("list_pages", {})
        self._require_generation(listing)
        return self._read_pages(listing)

    def _remember_page(
        self,
        name: str,
        args: Mapping[str, object],
        pages: Mapping[int, ListedPage],
        previous: Mapping[int, ListedPage],
    ) -> None:
        if name == "new_page":
            created = [
                page.id for page in pages.values() if page.selected and page.id not in previous
            ]
            if len(created) != 1:
                raise ValueError("Could not identify the page created by remote Chrome")
            self._current = created[0]
            self._pages.add(created[0])
        elif name == "select_page":
            self._current = page_number(args.get("pageId"))

    async def _upload(
        self,
        session: BrowserToolSession,
        uploads: list[AsyncExitStack],
        files: CallerFiles,
        original_path: str,
    ) -> object:
        if len(uploads) >= 8:
            raise ValueError("SSH Chrome reached the connection's upload staging limit")
        async with AsyncExitStack() as staging:
            path = await staging.enter_async_context(
                self._stage_upload(
                    self.config, str(files.args["filePath"]), files.inputs["filePath"]
                )
            )
            try:
                result = await self._paged_call(
                    session, "upload_file", {**files.args, "filePath": path}
                )
            except TimeoutError:
                uploads.append(staging.pop_all())
                raise
            if not result.is_error:
                uploads.append(staging.pop_all())
            result = replace_paths(result, {path: original_path})
            return remote_result("upload_file", files.args, result)["mcp"]

    async def call_tool(
        self,
        name: str,
        args: Mapping[str, object],
        *,
        workspace: Path,
    ) -> CallToolResult:
        files = CallerFiles(workspace, name, args)

        async def call(session: ClientSession, uploads: list[AsyncExitStack]) -> object:
            if files.inputs:
                return await self._upload(session, uploads, files, str(args["filePath"]))
            safe_args = {key: value for key, value in files.args.items() if key != "filePath"}
            result = await self._paged_call(session, name, safe_args)
            artifact = remote_result(name, files.args, result)
            return files.receive(
                CallToolResult.model_validate(artifact["mcp"]), artifact["files"]
            ).model_dump(mode="json")

        return CallToolResult.model_validate(await self._request(call))
