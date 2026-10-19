from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

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
_STREAM_END = object()


class AcpError(RuntimeError):
    """Base class for ACP transport failures."""


class AcpClosedError(AcpError):
    """The connection is gone: the child exited, closed stdout, or was closed."""


class AcpTimeoutError(AcpError):
    """A request did not answer within its deadline."""


class AcpRpcError(AcpError):
    """The peer answered a request with a JSON-RPC error."""

    def __init__(self, method: str, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"{method} failed with JSON-RPC error {code}: {message}")
        self.method = method
        self.code = code
        self.message = message
        self.data = data


@dataclass(frozen=True, slots=True)
class AcpNotification:
    method: str
    params: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PermissionOption:
    option_id: str
    name: str
    kind: str


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    session_id: str
    tool_call: dict[str, Any]
    options: tuple[PermissionOption, ...]

    def option(self, kind: str) -> PermissionOption | None:
        for candidate in self.options:
            if candidate.kind == kind:
                return candidate
        return None


# meiva:note
# A permission handler returns an option id or None. None is reported to the agent
# as a cancelled outcome, which the ACP contract lets it read as a refusal:
# inventing an allow would turn a missing approval surface into an unaudited grant.
PermissionHandler = Callable[[PermissionRequest], Awaitable[str | None]]


@dataclass(slots=True)
class _Pending:
    request_id: int
    method: str
    future: asyncio.Future[dict[str, Any]]


class AcpConnection:
    """Spawn one ACP agent over stdio and speak JSON-RPC with it."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        environ: Mapping[str, str],
        permission: PermissionHandler | None = None,
        request_timeout_seconds: float = 120.0,
        shutdown_timeout_seconds: float = 10.0,
        stderr_limit: int = MAX_STDERR_LINES,
    ) -> None:
        self.argv = tuple(argv)
        self.cwd = cwd
        self.environ = dict(environ)
        self.permission = permission
        self.request_timeout_seconds = request_timeout_seconds
        self.shutdown_timeout_seconds = shutdown_timeout_seconds
        self.stderr_tail: deque[str] = deque(maxlen=stderr_limit)
        self._process: asyncio.subprocess.Process | None = None
        self._stdin: asyncio.StreamWriter | None = None
        self._pending: dict[int, _Pending] = {}
        self._next_id = 0
        self._reader: asyncio.Task[None] | None = None
        self._stderr_reader: asyncio.Task[None] | None = None
        self._notifications: asyncio.Queue[Any] = asyncio.Queue()
        self._write_lock = asyncio.Lock()
        self._closed = False
        self._tasks = asyncio.TaskGroup()
        self._children: set[asyncio.Task[Any]] = set()
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
            # Own process group: teardown reaches the whole tree the agent spawned,
            # not only the process we hold a handle to.
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

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = self._tasks.create_task(coro)
        self._children.add(task)
        task.add_done_callback(self._children.discard)
        return task

    async def _close_tasks(self) -> None:
        children = [task for task in self._children if not task.done()]
        for task in children:
            task.cancel()
        if children:
            await asyncio.gather(*children, return_exceptions=True)
        if self._tasks_entered:
            self._tasks_entered = False
            with contextlib.suppress(ExceptionGroup):
                await self._tasks.__aexit__(None, None, None)

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
        frame: dict[str, Any] = {"jsonrpc": "2.0", "id": pending.request_id, "method": method}
        if params is not None:
            frame["params"] = dict(params)
        try:
            await self._write(frame)
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
        frame: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            frame["params"] = dict(params)
        await self._write(frame)

    async def notifications(self) -> AsyncIterator[AcpNotification]:
        while True:
            item = await self._notifications.get()
            if item is _STREAM_END:
                return
            yield cast("AcpNotification", item)

    async def close(self, *, reason: str = "closed") -> None:
        if self._closed:
            return
        self._closed = True
        self._notifications.put_nowait(_STREAM_END)
        self._fail_pending(AcpClosedError(f"connection closed: {reason}"))
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
        process = self._process
        if process is not None and process.returncode is None:
            if self._stdin is not None:
                with contextlib.suppress(Exception):
                    self._stdin.close()
            try:
                await asyncio.wait_for(process.wait(), timeout=self.shutdown_timeout_seconds)
            except TimeoutError:
                await self._terminate(process)
        await self._close_tasks()

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if process.returncode is not None:
                return
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(process.pid), sig)
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
                return
            except TimeoutError:
                continue

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
        payload = (json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8")
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
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                try:
                    frame = json.loads(text)
                except json.JSONDecodeError:
                    # A stray non-protocol line is a defect in the composition, not
                    # a reason to drop the connection; keep it for the postmortem.
                    self.stderr_tail.append(f"non-JSON stdout: {text[:300]}")
                    continue
                if isinstance(frame, dict):
                    await self._dispatch(frame)
        finally:
            self._closed = True
            self._fail_pending(AcpClosedError("the agent closed its output"))
            self._notifications.put_nowait(_STREAM_END)

    async def _dispatch(self, frame: dict[str, Any]) -> None:
        if "id" in frame and "method" in frame:
            await self._answer_server_request(frame)
            return
        if "method" in frame:
            params = frame.get("params")
            self._notifications.put_nowait(
                AcpNotification(
                    method=str(frame["method"]),
                    params=cast("dict[str, Any]", params) if isinstance(params, dict) else {},
                )
            )
            return
        request_id = frame.get("id")
        if not isinstance(request_id, int):
            return
        pending = self._pending.pop(request_id, None)
        if pending is None or pending.future.done():
            return
        error = frame.get("error")
        if isinstance(error, dict):
            pending.future.set_exception(
                AcpRpcError(
                    pending.method,
                    int(error.get("code", -1)),
                    str(error.get("message", "")),
                    error.get("data"),
                )
            )
            return
        result = frame.get("result")
        pending.future.set_result(
            cast("dict[str, Any]", result) if isinstance(result, dict) else {}
        )

    async def _answer_server_request(self, frame: dict[str, Any]) -> None:
        method = str(frame["method"])
        request_id = frame["id"]
        params = frame.get("params")
        if method == "session/request_permission" and isinstance(params, dict):
            await self._answer_permission(request_id, params)
            return
        await self._answer_error(request_id, -32601, f"client does not implement {method}")

    async def _answer_permission(self, request_id: Any, params: dict[str, Any]) -> None:
        options = tuple(
            PermissionOption(
                option_id=str(option.get("optionId", "")),
                name=str(option.get("name", "")),
                kind=str(option.get("kind", "")),
            )
            for option in cast("list[dict[str, Any]]", params.get("options") or [])
            if isinstance(option, dict)
        )
        request = PermissionRequest(
            session_id=str(params.get("sessionId", "")),
            tool_call=cast("dict[str, Any]", params.get("toolCall") or {}),
            options=options,
        )
        chosen: str | None = None
        if self.permission is not None:
            chosen = await self.permission(request)
        if chosen is None or chosen not in {option.option_id for option in options}:
            await self._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"outcome": {"outcome": "cancelled"}},
                }
            )
            return
        await self._write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"outcome": {"outcome": "selected", "optionId": chosen}},
            }
        )

    async def _answer_error(self, request_id: Any, code: int, message: str) -> None:
        await self._write(
            {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
        )

    async def _read_stderr(self, stderr: asyncio.StreamReader) -> None:
        while True:
            line = await stderr.readline()
            if not line:
                return
            self.stderr_tail.append(line.decode("utf-8", "replace").rstrip())

    def _fail_pending(self, exc: BaseException) -> None:
        for request_id, pending in list(self._pending.items()):
            del self._pending[request_id]
            if not pending.future.done():
                pending.future.set_exception(exc)
                pending.future.exception()
