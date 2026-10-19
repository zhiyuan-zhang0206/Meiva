from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from typing import Protocol, cast

JsonObject = dict[str, object]
RequestParams = Mapping[str, object]
RequestHandler = Callable[[str, JsonObject], Awaitable[JsonObject]]
NotificationHandler = Callable[[str, JsonObject], None]
ClosedHandler = Callable[[str], None]

# meiva:note
# One app-server payload can be a full command output or an item list, so the
# reader's line limit is far above asyncio's 64 KiB default: a longer line would
# raise ValueError inside readline() and kill the session stream.
# meiva:link base/lm/harness/codex/docs/codex.meiva.okf.md
STREAM_LIMIT_BYTES = 8 * 1024 * 1024


# meiva:note
# `TransportClosedError` means the child is gone, so no further request can be
# answered; `JsonRpcError` carries a server-side refusal the caller may recover
# from (a rejected `thread/resume`, for example).
class TransportClosedError(RuntimeError):
    pass


class JsonRpcError(RuntimeError):
    def __init__(self, code: int, message: str, data: object = None) -> None:
        super().__init__(f"app-server error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class AppServerTransport(Protocol):
    @property
    def pid(self) -> int | None: ...

    def on_notification(self, handler: NotificationHandler | None) -> None: ...

    def on_request(self, handler: RequestHandler | None) -> None: ...

    def on_closed(self, handler: ClosedHandler | None) -> None: ...

    async def start(self) -> None: ...

    async def request(
        self, method: str, params: RequestParams, *, timeout: float | None = None
    ) -> JsonObject: ...

    async def notify(self, method: str, params: RequestParams | None = None) -> None: ...

    async def close(self) -> None: ...


def as_object(value: object) -> JsonObject:
    return cast("JsonObject", value) if isinstance(value, dict) else {}


def as_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


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
        environ: Mapping[str, str] | None = None,
        request_timeout: float = 120.0,
        stderr: int | None = None,
    ) -> None:
        self._command = tuple(command)
        self._cwd = str(cwd)
        self._environ = dict(environ) if environ is not None else None
        self._request_timeout = request_timeout
        self._stderr = stderr
        self._process: asyncio.subprocess.Process | None = None
        self._tasks = asyncio.TaskGroup()
        self._children: set[asyncio.Task[None]] = set()
        self._tasks_entered = False
        self._pending: dict[int, asyncio.Future[JsonObject]] = {}
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
        future: asyncio.Future[JsonObject] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)}
            )
            reply = await asyncio.wait_for(
                future, timeout if timeout is not None else self._request_timeout
            )
        except TimeoutError:
            raise TimeoutError(f"app-server did not answer {method} in time") from None
        finally:
            self._pending.pop(request_id, None)
        error = reply.get("error")
        if error is not None:
            details = as_object(error)
            raise JsonRpcError(
                as_int(details.get("code")) or -32000,
                as_text(details.get("message")) or "unknown",
                details.get("data"),
            )
        return as_object(reply.get("result"))

    async def notify(self, method: str, params: RequestParams | None = None) -> None:
        message: JsonObject = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = dict(params)
        await self._write(message)

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
        await self._cancel_children()
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
        payload = (json.dumps(message, separators=(",", ":")) + "\n").encode()
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
                await self._dispatch(line)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._shutdown(f"app-server stream failed: {exc}")
            return
        self._shutdown("app-server closed its stdout")

    async def _dispatch(self, line: bytes) -> None:
        try:
            decoded: object = json.loads(line)
        except json.JSONDecodeError:
            return
        message = as_object(decoded)
        if not message:
            return
        if "id" in message and "method" in message:
            await self._answer(message)
            return
        if "id" in message:
            reply_id = as_int(message.get("id"))
            future = self._pending.get(reply_id) if reply_id is not None else None
            if future is not None and not future.done():
                future.set_result(message)
            return
        method = as_text(message.get("method"))
        handler = self._notification_handler
        if method is not None and handler is not None:
            handler(method, as_object(message.get("params")))

    async def _answer(self, message: JsonObject) -> None:
        request_id = message.get("id")
        method = as_text(message.get("method")) or ""
        params = as_object(message.get("params"))
        handler = self._request_handler
        if handler is None:
            await self._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": "no handler"},
                }
            )
            return
        try:
            result = await handler(method, params)
        except Exception as exc:
            await self._write(
                {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": str(exc)}}
            )
            return
        await self._write({"jsonrpc": "2.0", "id": request_id, "result": result})

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

    async def _cancel_children(self) -> None:
        children = [task for task in self._children if not task.done()]
        for task in children:
            task.cancel()
        if children:
            await asyncio.gather(*children, return_exceptions=True)

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
