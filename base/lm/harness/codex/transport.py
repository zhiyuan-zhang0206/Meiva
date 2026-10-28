from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
from collections.abc import Awaitable, Coroutine, Mapping, Sequence
from typing import Protocol

from base.lm.harness.codex.frames import (
    ClosedHandler,
    JsonObject,
    JsonRpcError,
    Notification,
    NotificationHandler,
    Reply,
    RequestHandler,
    RequestParams,
    ServerRequest,
    TransportClosedError,
    classify,
    encode,
    notification_frame,
    request_frame,
)

# meiva:note
# One app-server payload can be a full command output or an item list, so the
# reader's line limit is far above asyncio's 64 KiB default: a longer line would
# raise ValueError inside readline() and kill the session stream.
# meiva:link base/lm/harness/codex/docs/codex.meiva.okf.md
STREAM_LIMIT_BYTES = 8 * 1024 * 1024


class AppServerTransport(Protocol):
    @property
    def pid(self) -> int | None: ...

    # meiva:note
    # The CLI child's exit status in three states: None while it runs or when no child
    # was started, 0 normal, non-zero abnormal. The session records it on close, after
    # the process has been reaped (task #5616).
    @property
    def returncode(self) -> int | None: ...

    def on_notification(self, handler: NotificationHandler | None) -> None: ...

    def on_request(self, handler: RequestHandler | None) -> None: ...

    def on_closed(self, handler: ClosedHandler | None) -> None: ...

    def start(self) -> Awaitable[None]: ...

    def request(
        self, method: str, params: RequestParams, *, timeout: float | None = None
    ) -> Awaitable[JsonObject]: ...

    def notify(self, method: str, params: RequestParams | None = None) -> Awaitable[None]: ...

    def close(self) -> Awaitable[None]: ...


# meiva:note
# JSON-RPC over the app-server's stdio transport: one JSON object per line, ids
# correlate replies, and server-initiated requests (approvals) must be answered on
# the same pipe or the turn parks forever. Replies and notifications interleave
# with those requests, so a single reader loop owns dispatch and everything the
# session sees arrives through the two injected handlers.
# meiva:link base/lm/harness/codex/docs/codex.meiva.okf.md
class StdioAppServer:
    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: str | os.PathLike[str],
        environ: Mapping[str, str],
        request_timeout: float = 120.0,
        stderr: int | None = None,
    ) -> None:
        self._command = tuple(command)
        self._cwd = str(cwd)
        self._environ = dict(environ)
        self._request_timeout = request_timeout
        self._stderr = stderr
        self._process: asyncio.subprocess.Process | None = None
        self._tasks = asyncio.TaskGroup()
        self._children: set[asyncio.Task[None]] = set()
        self._tasks_entered = False
        self._pending: dict[int, asyncio.Future[Reply]] = {}
        self._next_id = 0
        self._write_lock = asyncio.Lock()
        self._notification_handler: NotificationHandler | None = None
        self._request_handler: RequestHandler | None = None
        self._closed_handler: ClosedHandler | None = None
        self._closed = False
        self._closing = False

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    # meiva:note
    # The exit status of the CLI child, same three states as everywhere else: None
    # while it runs or when no child was ever started, 0 normal, non-zero abnormal.
    # The session records it on close, after this process has been reaped.
    @property
    def returncode(self) -> int | None:
        return None if self._process is None else self._process.returncode

    @property
    def closed(self) -> bool:
        return self._closed

    def on_notification(self, handler: NotificationHandler | None) -> None:
        self._notification_handler = handler

    def on_request(self, handler: RequestHandler | None) -> None:
        self._request_handler = handler

    def on_closed(self, handler: ClosedHandler | None) -> None:
        self._closed_handler = handler

    async def start(self) -> None:
        if self._process is not None:
            raise RuntimeError("transport already started")
        self._process = await asyncio.create_subprocess_exec(
            *self._command,
            cwd=self._cwd,
            env=self._environ,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=self._stderr,
            limit=STREAM_LIMIT_BYTES,
        )
        await self._tasks.__aenter__()
        self._tasks_entered = True
        self._spawn(self._read_loop())

    async def request(
        self, method: str, params: RequestParams, *, timeout: float | None = None
    ) -> JsonObject:
        if self._closed:
            raise TransportClosedError(f"transport closed before {method}")
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[Reply] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write(request_frame(request_id, method, params))
            reply = await asyncio.wait_for(
                future, timeout if timeout is not None else self._request_timeout
            )
        except TimeoutError:
            raise TimeoutError(f"app-server did not answer {method} in time") from None
        finally:
            self._pending.pop(request_id, None)
        outcome = reply.outcome()
        if isinstance(outcome, JsonRpcError):
            raise outcome
        return outcome

    async def notify(self, method: str, params: RequestParams | None = None) -> None:
        await self._write(notification_frame(method, params))

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._closed = True
        process = self._process
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5.0)
            except TimeoutError:
                process.kill()
                with contextlib.suppress(ProcessLookupError):
                    await process.wait()
        children = [task for task in self._children if not task.done()]
        for task in children:
            task.cancel()
        if children:
            await asyncio.gather(*children, return_exceptions=True)
        if self._tasks_entered:
            self._tasks_entered = False
            try:
                await self._tasks.__aexit__(None, None, None)
            except ExceptionGroup as group:
                for error in group.exceptions:
                    if self._closed_handler is not None:
                        self._closed_handler(f"app-server task failed: {error}")
        self._fail_pending("transport closed")

    async def _write(self, message: JsonObject) -> None:
        process = self._process
        if process is None or process.stdin is None or self._closed:
            raise TransportClosedError("app-server is not running")
        payload = encode(message)
        async with self._write_lock:
            process.stdin.write(payload)
            await process.stdin.drain()

    async def _read_loop(self) -> None:
        if self._process is None or self._process.stdout is None:
            raise RuntimeError("read loop started before the process")
        stdout = self._process.stdout
        try:
            while True:
                line = await stdout.readline()
                if not line:
                    break
                match classify(line):
                    case ServerRequest() as request:
                        await self._serve(request)
                    case Reply() as reply:
                        self._settle(reply)
                    case Notification() as notification:
                        self._notify(notification)
                    case None:
                        pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._shutdown(f"app-server stream failed: {exc}")
            return
        self._shutdown("app-server closed its stdout")

    async def _serve(self, request: ServerRequest) -> None:
        handler = self._request_handler
        if handler is None:
            await self._write(request.refused())
            return
        try:
            answer = handler(request.method, request.params)
            result = await answer if inspect.isawaitable(answer) else answer
        except Exception as exc:
            await self._write(request.failed(exc))
            return
        await self._write(request.answered(result))

    def _settle(self, reply: Reply) -> None:
        future = self._pending.get(reply.request_id)
        if future is not None and not future.done():
            future.set_result(reply)

    def _notify(self, notification: Notification) -> None:
        handler = self._notification_handler
        if handler is not None:
            handler(notification.method, notification.params)

    def _spawn(self, coro: Coroutine[object, object, None]) -> asyncio.Task[None]:
        task = self._tasks.create_task(coro)
        self._children.add(task)
        task.add_done_callback(self._on_task_done)
        return task

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        self._children.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._shutdown(f"app-server stream failed: {error}")

    def _shutdown(self, reason: str) -> None:
        if self._closed:
            return
        self._closed = True
        self._fail_pending(reason)
        if self._closed_handler is not None:
            self._closed_handler(reason)

    def _fail_pending(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(TransportClosedError(reason))
                future.exception()
        self._pending.clear()
