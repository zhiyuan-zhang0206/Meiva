from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import signal
from collections import deque
from collections.abc import AsyncIterator, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from base.lm.harness.acp.errors import AcpClosedError, AcpError, AcpRpcError, AcpTimeoutError
from base.lm.harness.acp.frames import (
    Frame,
    NonJson,
    decode_line,
    encode_frame,
    non_json_note,
    notification_frame,
    request_frame,
    stderr_text,
)
from base.lm.harness.acp.inbound import (
    AcpNotification,
    AskPermission,
    Refusal,
    Reply,
    ServerRequest,
    answer_server_request,
    classify,
    reply_outcome,
)
from base.lm.harness.acp.permission import PermissionHandler, permission_response

# meiva:note
# One ACP connection is one child process and one newline-delimited JSON-RPC
# stream. Three frame shapes share it: a request carries `id` and `method`, a
# response carries `id` alone, and a notification carries `method` alone. The
# agent also makes requests of us (permission first), so the reader dispatches in
# both directions while a request of ours is pending.
#
# The transport owns no policy. It answers only what a handler answers and refuses
# every other client-side method with -32601: silently accepting a surface we do
# not implement would let an agent believe it read a file or ran a command.
# meiva:link base/lm/harness/acp/docs/acp.meiva.okf.md
MAX_STDERR_LINES = 200


@dataclass(frozen=True, slots=True)
class _Pending:
    request_id: int
    method: str
    future: asyncio.Future[dict[str, Any]]


class AcpConnection:
    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        environ: Mapping[str, str],
        permission: PermissionHandler | None = None,
        request_timeout_seconds: float = 120.0,
        shutdown_timeout_seconds: float = 10.0,
    ) -> None:
        self.argv = tuple(argv)
        self.cwd = cwd
        self.environ = dict(environ)
        self.permission = permission
        self.request_timeout_seconds = request_timeout_seconds
        self.shutdown_timeout_seconds = shutdown_timeout_seconds
        self.stderr_tail: deque[str] = deque(maxlen=MAX_STDERR_LINES)
        self._process: asyncio.subprocess.Process | None = None
        self._stdin: asyncio.StreamWriter | None = None
        self._pending: dict[int, _Pending] = {}
        self._next_id = 0
        self._reader: asyncio.Task[None] | None = None
        self._stderr_reader: asyncio.Task[None] | None = None
        self._notifications: asyncio.Queue[AcpNotification | None] = asyncio.Queue()
        self._write_lock = asyncio.Lock()
        self._closed = False
        self._close_complete = False
        self._close_lock = asyncio.Lock()
        self._tasks = asyncio.TaskGroup()
        self._children: set[asyncio.Task[None]] = set()
        self._tasks_entered = False

    async def start(self) -> None:
        if self._process is not None:
            raise AcpError("connection already started")
        process = await asyncio.create_subprocess_exec(
            *self.argv,
            cwd=self.cwd,
            env=self.environ,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        stdin, stdout, stderr = process.stdin, process.stdout, process.stderr
        if stdin is None or stdout is None or stderr is None:
            raise AcpError("the child did not expose stdio pipes")
        self._process = process
        self._stdin = stdin
        if not self._tasks_entered:
            await self._tasks.__aenter__()
            self._tasks_entered = True
        self._reader = self._spawn(self._read_loop(stdout))
        self._stderr_reader = self._spawn(self._read_stderr(stderr))

    def _spawn(self, coro: Coroutine[object, object, None]) -> asyncio.Task[None]:
        task = self._tasks.create_task(coro)
        self._children.add(task)
        task.add_done_callback(self._children.discard)
        return task

    @property
    def pid(self) -> int | None:
        return None if self._process is None else self._process.pid

    @property
    def returncode(self) -> int | None:
        return None if self._process is None else self._process.returncode

    @property
    def closed(self) -> bool:
        return self._closed

    async def request(
        self, method: str, params: Mapping[str, Any] | None = None, *, timeout: float | None = None
    ) -> dict[str, Any]:
        pending = self._register(method)
        try:
            await self._write(request_frame(pending.request_id, method, params))
        except AcpError:
            self._pending.pop(pending.request_id, None)
            raise
        deadline = self.request_timeout_seconds if timeout is None else timeout
        try:
            return await asyncio.wait_for(pending.future, timeout=deadline)
        except TimeoutError as exc:
            self._pending.pop(pending.request_id, None)
            raise AcpTimeoutError(f"{method} did not answer within {deadline:.0f}s") from exc

    async def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        await self._write(notification_frame(method, params))

    async def notifications(self) -> AsyncIterator[AcpNotification]:
        while True:
            item = await self._notifications.get()
            if item is None:
                return
            yield item

    async def close(self, *, reason: str = "closed") -> None:
        # meiva:note
        # Stdout EOF closes admission before the child necessarily exits. Only a
        # completed teardown may skip the process wait and retained task cleanup.
        # A cancelled caller leaves completion false for the next close to finish.
        async with self._close_lock:
            if self._close_complete:
                return
            self._closed = True
            self._notifications.put_nowait(None)
            self._fail_pending(AcpClosedError(f"connection closed: {reason}"))
            if self._reader is not None:
                self._reader.cancel()
                await asyncio.wait({self._reader})
                with contextlib.suppress(asyncio.CancelledError):
                    self._reader.result()
            process = self._process
            if process is not None and process.returncode is None:
                if self._stdin is not None:
                    with contextlib.suppress(OSError, RuntimeError):
                        self._stdin.close()
                try:
                    await asyncio.wait_for(process.wait(), timeout=self.shutdown_timeout_seconds)
                except TimeoutError:
                    for sig in (signal.SIGTERM, signal.SIGKILL):
                        if process.returncode is not None:
                            break
                        with contextlib.suppress(ProcessLookupError, PermissionError):
                            os.killpg(os.getpgid(process.pid), sig)
                        try:
                            await asyncio.wait_for(process.wait(), timeout=5.0)
                            break
                        except TimeoutError:
                            continue
            children = self._cancel_active_children()
            if children:
                await asyncio.gather(*children, return_exceptions=True)
            if self._tasks_entered:
                self._tasks_entered = False
                with contextlib.suppress(ExceptionGroup):
                    await self._tasks.__aexit__(None, None, None)
            self._close_complete = True

    def _cancel_active_children(self) -> list[asyncio.Task[None]]:
        children = [task for task in self._children if not task.done()]
        for task in children:
            task.cancel()
        return children

    def _register(self, method: str) -> _Pending:
        if self._closed or self._process is None or self._process.returncode is not None:
            raise AcpClosedError("connection is not open")
        self._next_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        pending = _Pending(request_id=self._next_id, method=method, future=future)
        self._pending[self._next_id] = pending
        return pending

    async def _write(self, frame: Mapping[str, Any]) -> None:
        stdin = self._stdin
        if stdin is None or self._closed:
            raise AcpClosedError("connection is not open")
        payload = encode_frame(frame)
        async with self._write_lock:
            try:
                stdin.write(payload)
                await stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise AcpClosedError("the agent closed its input") from exc

    async def _read_loop(self, stdout: asyncio.StreamReader) -> None:
        try:
            while True:
                line = await stdout.readline()
                if not line:
                    break
                decoded = decode_line(line)
                if isinstance(decoded, NonJson):
                    self.stderr_tail.append(non_json_note(decoded.text))
                    continue
                if not isinstance(decoded, Frame):
                    continue
                match classify(decoded.value):
                    case None:
                        continue
                    case AcpNotification() as notification:
                        self._notifications.put_nowait(notification)
                    case Reply() as reply:
                        self._settle(reply)
                    case ServerRequest() as inbound:
                        match answer_server_request(inbound):
                            case Refusal(frame):
                                await self._write(frame)
                            case AskPermission(request, request_id):
                                reply_to = (
                                    self.permission(request)
                                    if self.permission is not None
                                    else None
                                )
                                chosen = (
                                    await reply_to if inspect.isawaitable(reply_to) else reply_to
                                )
                                await self._write(
                                    permission_response(request_id, chosen, request.options)
                                )
        finally:
            self._closed = True
            self._fail_pending(AcpClosedError("the agent closed its output"))
            self._notifications.put_nowait(None)

    def _settle(self, reply: Reply) -> None:
        pending = self._pending.pop(reply.request_id, None)
        if pending is None or pending.future.done():
            return
        outcome = reply_outcome(reply, pending.method)
        if isinstance(outcome, AcpRpcError):
            pending.future.set_exception(outcome)
        else:
            pending.future.set_result(outcome)

    async def _read_stderr(self, stderr: asyncio.StreamReader) -> None:
        while True:
            line = await stderr.readline()
            if not line:
                return
            self.stderr_tail.append(stderr_text(line))

    def _fail_pending(self, exc: BaseException) -> None:
        for request_id, pending in list(self._pending.items()):
            del self._pending[request_id]
            if not pending.future.done():
                pending.future.set_exception(exc)
                pending.future.exception()
