# meiva:link meiva_builtins/mcps/docs/chrome/chrome.md

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack, suppress
from pathlib import Path

from mcp import ClientSession
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

from base.clock import Timers
from base.host.remote_hosts import RemoteHostConfig
from services.desktop.browser.mcp_upstream import create_upstream
from services.desktop.remote.ssh_tunnel import SshTunnelManager

from .remote_files import CallerFiles, replace_paths, stage_files
from .remote_pages import (
    PAGE_LIST_TOOLS,
    authorize_page,
    page_number,
    scope_page_list,
    selected_page,
)
from .remote_rpc import RPC_TIMEOUT_S
from .ssh_upload import stage_ssh_upload

type BrowserAction = Callable[[ClientSession, AsyncExitStack], Awaitable[object]]


class SshBrowser:
    def __init__(
        self,
        config: RemoteHostConfig,
        *,
        timers: Timers,
        tasks: asyncio.TaskGroup,
        connect_timeout_s: float,
    ) -> None:
        self.config = config
        self._timers = timers
        self._tasks = tasks
        self._timeout_s = connect_timeout_s
        self._queue: asyncio.Queue[tuple[BrowserAction, asyncio.Future[object]]] = asyncio.Queue()
        self._task: asyncio.Task[Exception | None] | None = None
        self._pages: set[int] = set()
        self._current: int | None = None
        self._uploads = 0

    async def _run(self, ready: asyncio.Future[None]) -> Exception | None:
        tunnel = SshTunnelManager(timers=self._timers)
        try:
            endpoint = await asyncio.to_thread(tunnel.endpoint_for, self.config, browser=True)
            session, upstream = await create_upstream(
                endpoint, asyncio.Event(), connect_timeout_reader=lambda: self._timeout_s
            )
            async with upstream, AsyncExitStack() as uploads:
                if ready.cancelled():
                    return None
                ready.set_result(None)
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
        except Exception as exc:
            if not ready.done():
                ready.set_exception(exc)
            return exc
        finally:
            await asyncio.to_thread(tunnel.close_all)

    async def close(self) -> None:
        task, self._task = self._task, None
        self._pages.clear()
        self._current = None
        self._uploads = 0
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                _ = await task

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
        async def list_tools(session: ClientSession, _uploads: AsyncExitStack) -> object:
            return await session.list_tools()

        return ListToolsResult.model_validate(await self._request(list_tools)).tools

    async def _paged_call(
        self,
        session: ClientSession,
        name: str,
        args: dict[str, object],
    ) -> CallToolResult:
        authorize_page(name, args, self._pages.__contains__)
        if (
            (name not in PAGE_LIST_TOOLS or name == "navigate_page")
            and self._current is None
            and "pageId" not in args
        ):
            if name == "navigate_page" and isinstance(args.get("url"), str):
                name = "new_page"
            else:
                raise ValueError("No remote Chrome page selected. Open a page first.")
        if name == "new_page":
            args = {"background": True, **args}
        result = await session.call_tool(name, args)
        if result.is_error:
            return result
        self._remember_page(name, args, result)
        if name in PAGE_LIST_TOOLS:
            return scope_page_list(result, self._pages.__contains__, self._current)
        return result

    def _remember_page(self, name: str, args: Mapping[str, object], result: CallToolResult) -> None:
        if name == "new_page":
            if (page := selected_page(result)) is None:
                raise RuntimeError("Could not identify the page created by remote Chrome")
            self._current = page
            self._pages.add(page)
        elif name == "select_page":
            self._current = page_number(args.get("pageId"))
        elif name == "close_page":
            page = page_number(args.get("pageId"))
            if page is not None and not any(
                isinstance(item, TextContent) and f"\n{page}: " in f"\n{item.text}"
                for item in result.content
            ):
                self._pages.discard(page)
                if self._current == page:
                    self._current = None

    async def call_tool(
        self,
        name: str,
        args: Mapping[str, object],
        *,
        workspace: Path,
    ) -> CallToolResult:
        files = CallerFiles(workspace, name, args)

        async def call(session: ClientSession, uploads: AsyncExitStack) -> object:
            if files.inputs:
                if self._uploads >= 32:
                    raise ValueError("SSH Chrome reached the connection's upload staging limit")
                async with AsyncExitStack() as staging:
                    path = await staging.enter_async_context(
                        stage_ssh_upload(
                            self.config,
                            str(files.args["filePath"]),
                            files.inputs["filePath"],
                        )
                    )
                    result = await self._paged_call(session, name, {**files.args, "filePath": path})
                    if not result.is_error:
                        _ = uploads.push_async_callback(staging.pop_all().aclose)
                        self._uploads += 1
                    return replace_paths(result, {path: str(args["filePath"])}).model_dump(
                        mode="json"
                    )
            with stage_files(name, files.args, {}, parent=workspace) as staged:
                result = await self._paged_call(session, name, staged.args)
                if result.is_error:
                    return result.model_dump(mode="json")
                return files.receive(result, staged.outputs()).model_dump(mode="json")

        return CallToolResult.model_validate(await self._request(call))
