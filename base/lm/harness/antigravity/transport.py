from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import signal
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import replace
from pathlib import Path

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_of
from base.host.env.harness_binaries import resolve_harness_binary
from base.lm.harness.antigravity.config import MINIMUM_ACP_VERSION, AntigravityConfig
from base.lm.harness.antigravity.rpc import (
    LIVE_SUBMIT_TIMEOUT_SECONDS,
    AntigravityServer,
    discover_process_antigravity_server,
    get_cascade_trajectory,
)
from base.lm.harness.antigravity.timing_store import store_timings
from base.lm.harness.antigravity.trajectory import Trajectory, parse_trajectory
from base.lm.harness.antigravity.transcript.window import session_file
from base.lm.harness.process import ProcessFamily, PsutilChildProbe, WaitClock
from base.log import logger

READ_CHUNK_BYTES = 65536

# meiva:note
# A single NDJSON line can carry a large tool result or accumulated turn state;
# chunked 64 KiB reads assemble lines without `StreamReader.readline()`'s 8 MiB
# separator limit while capping one frame at 256 MiB so an unterminated stream
# from a wedged child cannot grow memory without bound.
# meiva:link base/lm/harness/antigravity/docs/antigravity.md
MAX_FRAME_BYTES = 256 * 1024 * 1024
CANCEL_POLL_SECONDS = 0.02
STDERR_TAIL_LINES = 40


async def _iter_lines(
    stream: asyncio.StreamReader, *, max_frame_bytes: int
) -> AsyncIterator[bytes]:
    buffer = bytearray()
    while True:
        chunk = await stream.read(READ_CHUNK_BYTES)
        if not chunk:
            if buffer:
                yield bytes(buffer)
            return
        if b"\n" not in chunk:
            if len(buffer) + len(chunk) > max_frame_bytes:
                raise ValueError(f"stream line exceeded {max_frame_bytes} bytes")
            buffer.extend(chunk)
            continue
        parts = chunk.split(b"\n")
        if len(buffer) + len(parts[0]) > max_frame_bytes:
            raise ValueError(f"stream line exceeded {max_frame_bytes} bytes")
        if buffer:
            buffer.extend(parts[0])
            first = bytes(buffer)
            buffer = bytearray()
        else:
            first = parts[0]
        yield first
        for middle in parts[1:-1]:
            if len(middle) > max_frame_bytes:
                raise ValueError(f"stream line exceeded {max_frame_bytes} bytes")
            yield middle
        tail = parts[-1]
        if len(tail) > max_frame_bytes:
            raise ValueError(f"stream line exceeded {max_frame_bytes} bytes")
        if tail:
            buffer.extend(tail)


async def _drain_pipe(stream: asyncio.StreamReader) -> None:
    """Release pipe backpressure while the owner closes an invalid stream."""
    while await stream.read(READ_CHUNK_BYTES):
        pass


def _permission_outcome(params: dict[str, object]) -> dict[str, object]:
    options = params.get("options")
    if is_object_list(options):
        for kind in ("reject_once", "reject_always"):
            for option in options:
                if is_object_dict(option) and option.get("kind") == kind:
                    option_id = option.get("optionId")
                    if isinstance(option_id, str):
                        return {"outcome": {"outcome": "selected", "optionId": option_id}}
    return {"outcome": {"outcome": "cancelled"}}


class TransportClosedError(RuntimeError):
    pass


class AgyTransport:
    def __init__(
        self,
        config: AntigravityConfig,
        *,
        environ: Mapping[str, str] | None = None,
        clock: WaitClock,
        max_frame_bytes: int = MAX_FRAME_BYTES,
    ) -> None:
        self._config = config
        self._clock = clock
        self._max_frame_bytes = max_frame_bytes
        self._environ = dict(environ) if environ else None
        csrf_seed = (
            (self._environ.get("MEIVA_HARNESS_HANDLE") or "") if self._environ is not None else ""
        ) or str(config.cwd)
        self._csrf_token = (
            (self._environ.get("ANTIGRAVITY_SERVER_CSRF_TOKEN") or "").strip()
            if self._environ is not None
            else ""
        ) or hashlib.sha256(csrf_seed.encode("utf-8")).hexdigest()[:32]
        if self._environ is not None:
            self._environ["ANTIGRAVITY_SERVER_CSRF_TOKEN"] = self._csrf_token
        self._process: asyncio.subprocess.Process | None = None
        self._stdio: asyncio.SubprocessTransport | None = None
        self._family: ProcessFamily | None = None
        self._returncode: int | None = None
        self._queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()
        self._tasks = asyncio.TaskGroup()
        self._tasks_entered = False
        self._pumps: list[asyncio.Task[None]] = []
        self._stderr: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        self._conversation_id: str | None = None
        self._request_id = 0
        self._setup: dict[int, asyncio.Future[dict[str, object]]] = {}
        self._live_request: int | None = None
        self._close_lock = asyncio.Lock()
        self._session_ready = False
        self._stream_closed = False
        self._rpc_server: AntigravityServer | None = None

    @classmethod
    def create(
        cls, config: AntigravityConfig, *, environ: Mapping[str, str] | None, clock: WaitClock
    ) -> AgyTransport:
        resolved = resolve_harness_binary(
            config.executable[0], path_env=(environ or {}).get("PATH")
        )
        config = replace(config, executable=(resolved, *config.executable[1:]))
        return cls(config, environ=environ, clock=clock)

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    # meiva:note
    # The exit status of the CLI child, same three states as everywhere else: None
    # while it runs or when no child was ever started, 0 normal, non-zero abnormal.
    # The session records it on close, after this process has been reaped.
    @property
    def returncode(self) -> int | None:
        return self._returncode if self._process is None else self._process.returncode

    @property
    def conversation_id(self) -> str | None:
        return self._conversation_id

    @property
    def stderr_tail(self) -> tuple[str, ...]:
        return tuple(self._stderr)

    async def start(self) -> None:
        if self._process is not None:
            raise RuntimeError("transport already started")
        argv = self._config.command()
        loop = asyncio.get_running_loop()
        protocol = asyncio.subprocess.SubprocessStreamProtocol(limit=READ_CHUNK_BYTES, loop=loop)
        io, _ = await loop.subprocess_exec(
            lambda: protocol,
            *argv,
            cwd=str(self._config.cwd),
            env=self._environ,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        process = asyncio.subprocess.Process(io, protocol, loop)
        self._process = process
        self._stdio = io
        self._family = ProcessFamily(process.pid, PsutilChildProbe())
        if process.stdout is None or process.stderr is None:
            raise RuntimeError("agy stdio pipes are not available")
        if not self._tasks_entered:
            await self._tasks.__aenter__()
            self._tasks_entered = True
        self._pumps = [
            self._tasks.create_task(self._pump_stdout(process.stdout)),
            self._tasks.create_task(self._pump_stderr(process.stderr)),
        ]

    async def open_session(self, *, resume: str | None) -> str:
        try:
            initialized = await self._request(
                "initialize", {"protocolVersion": 1, "clientCapabilities": {}}
            )
        except (TransportClosedError, OSError) as exc:
            raise await self._startup_error(exc) from exc
        self._require_acp_version(initialized)
        params: dict[str, object] = {"cwd": str(self._config.cwd), "mcpServers": []}
        if resume is not None:
            params["sessionId"] = resume
        result = await self._request("session/load" if resume else "session/new", params)
        # session/load can omit sessionId and sends historical updates before the response.
        session_id = result.get("sessionId", resume)
        if not isinstance(session_id, str) or not session_id or (resume and session_id != resume):
            raise RuntimeError("agy returned an unexpected ACP session id")
        self._conversation_id = session_id
        _ = await self._request(
            "session/set_model", {"sessionId": session_id, "modelId": self._config.model}
        )
        return session_id

    async def _startup_error(self, error: BaseException) -> TransportClosedError:
        process = self._require_process()
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(self._config.cancel_settle_seconds):
                await process.wait()
                _ = await asyncio.wait(self._pumps)
        minimum = ".".join(map(str, MINIMUM_ACP_VERSION))
        stderr = "\n".join(self.stderr_tail)
        return TransportClosedError(
            f"agy ACP startup failed (requires agy {minimum} or later, exit={process.returncode}): "
            f"{error}\n{stderr}"
        )

    def _require_acp_version(self, initialized: dict[str, object]) -> None:
        if initialized.get("protocolVersion") != 1:
            raise RuntimeError("agy does not support ACP protocol version 1")
        info = initialized.get("agentInfo")
        version = info.get("version") if is_object_dict(info) else None
        match = (
            re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:\+.+)?", version)
            if isinstance(version, str)
            else None
        )
        if match is None or tuple(map(int, match.groups())) < MINIMUM_ACP_VERSION:
            minimum = ".".join(map(str, MINIMUM_ACP_VERSION))
            raise RuntimeError(
                f"lossless ACP steering requires agy {minimum} or later, got {version!r}"
            )

    async def _request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        request_id = self.next_request_id()
        future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
        self._setup[request_id] = future
        if method in ("session/load", "session/new"):
            self._live_request = request_id
        try:
            async with asyncio.timeout(self._config.startup_request_seconds):
                self._write({"id": request_id, "method": method, "params": params})
                await self.flush()
                response = await future
        finally:
            del self._setup[request_id]
        result = response.get("result")
        if not is_object_dict(result):
            raise RuntimeError(f"agy ACP {method} failed: {response.get('error')!r}")
        return result

    def next_request_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def prompt(
        self, request_id: int, blocks: list[dict[str, str]], *, before_write: Callable[[], None]
    ) -> None:
        self._write(
            {
                "id": request_id,
                "method": "session/prompt",
                "params": {"sessionId": self._conversation_id, "prompt": blocks},
            },
            before_write=before_write,
        )

    def cancel(self) -> None:
        self._write({"method": "session/cancel", "params": {"sessionId": self._conversation_id}})

    def reject_permission(self, request_id: int | str, params: dict[str, object]) -> None:
        self._write({"id": request_id, "result": _permission_outcome(params)})

    def respond_error(self, request_id: int | str) -> None:
        self._write({"id": request_id, "error": {"code": -32601, "message": "Method not found"}})

    def _write(
        self, payload: dict[str, object], *, before_write: Callable[[], None] | None = None
    ) -> None:
        stream = self._require_process().stdin
        if stream is None or not self.alive or self._stream_closed:
            raise TransportClosedError("agy ACP stream is closed")
        data = (json.dumps({"jsonrpc": "2.0", **payload}) + "\n").encode()
        if before_write is not None:
            before_write()
        stream.write(data)

    async def flush(self) -> None:
        stream = self._require_process().stdin
        if stream is None:
            raise TransportClosedError("agy stdin is missing")
        await stream.drain()

    async def flush_prompt(self) -> None:
        async with asyncio.timeout(self._config.prompt_write_seconds):
            await self.flush()

    def buffered_events(self) -> tuple[dict[str, object], ...]:
        frames: list[dict[str, object]] = []
        while not self._queue.empty():
            frame = self._queue.get_nowait()
            if frame is None:
                self._queue.put_nowait(None)
                break
            frames.append(frame)
        return tuple(frames)

    def _timing_record(self, conversation_id: str | None = None) -> Path | None:
        target = conversation_id or self._conversation_id
        if not target or self._environ is None or not self._environ.get("HOME"):
            return None
        return session_file(self._environ, target)

    def has_timing_record(self, *, conversation_id: str | None = None) -> bool:
        try:
            return self._timing_record(conversation_id) is not None
        except OSError:
            return False

    # meiva:note
    # The live state of the conversation: `GetCascadeTrajectory` exposes both the top-level run status
    # and child-step statuses while stdout is silent. An unexpected shape raises `AgyInterfaceError`.
    async def trajectory(self, *, conversation_id: str | None = None) -> Trajectory:
        server, target_conversation = await self._rpc_target(conversation_id)
        payload = await asyncio.to_thread(
            get_cascade_trajectory, target_conversation, server=server
        )
        trajectory = parse_trajectory(payload)
        try:
            record = self._timing_record(target_conversation)
            if record is not None:
                await asyncio.to_thread(store_timings, record, trajectory)
        except OSError:
            logger.opt(exception=True).warning(
                "native timing record could not be located; provider execution is unchanged",
                event="harness_timing_record_unavailable",
            )
        return trajectory

    async def _rpc_target(self, conversation_id: str | None) -> tuple[AntigravityServer, str]:
        process = self._require_process()
        if process.returncode is not None:
            raise TransportClosedError("agy is not running")
        target_conversation = conversation_id or self._conversation_id
        if process.returncode is not None or not target_conversation:
            raise TransportClosedError("agy is not running")
        server = self._rpc_server
        if server is None:
            server = await self._discover_rpc_server(process.pid)
            self._rpc_server = server
        return server, target_conversation

    async def _discover_rpc_server(self, pid: int) -> AntigravityServer:
        deadline = self._clock.monotonic() + LIVE_SUBMIT_TIMEOUT_SECONDS
        last_error: RuntimeError | None = None
        while True:
            try:
                return await asyncio.to_thread(
                    discover_process_antigravity_server,
                    pid,
                    self._csrf_token,
                )
            except RuntimeError as exc:
                last_error = exc
            if self._clock.monotonic() >= deadline:
                raise last_error
            await asyncio.sleep(CANCEL_POLL_SECONDS)

    def observe_children(self) -> None:
        if self._family is not None:
            self._family.observe()

    async def close(self, *, broken: bool = False, agent_id: int | None = None) -> None:
        async with self._close_lock:
            await self._close_process(broken=broken, agent_id=agent_id)

    async def _close_process(self, *, broken: bool, agent_id: int | None) -> None:
        process, io, family = self._process, self._stdio, self._family
        try:
            if process is not None and io is not None:
                try:
                    if family is not None:
                        family.observe_session()
                    if not broken:
                        await self._request_exit(process)
                finally:
                    stdin = io.get_pipe_transport(0)
                    if isinstance(stdin, asyncio.WriteTransport) and not stdin.is_closing():
                        stdin.abort()
                    io.close()
                    try:
                        if family is not None:
                            family.observe_session()
                            family.kill()
                    finally:
                        await self._close_streams(process)
        finally:
            if family is not None:
                await family.settle(
                    harness="antigravity",
                    agent_id=agent_id,
                    timeout=self._config.cancel_settle_seconds,
                    clock=self._clock,
                    poll_seconds=CANCEL_POLL_SECONDS,
                )
                self._family = None

    async def _request_exit(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.send_signal(signal.SIGINT)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=self._config.cancel_settle_seconds)

    async def _close_streams(self, process: asyncio.subprocess.Process) -> None:
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=self._config.cancel_settle_seconds)
        finally:
            for pump in self._pumps:
                pump.cancel()
            self._pumps = []
            if self._tasks_entered:
                self._tasks_entered = False
                with contextlib.suppress(ExceptionGroup):
                    await self._tasks.__aexit__(None, None, None)
            self._returncode = process.returncode
            self._process = None
            self._stdio = None
            self._rpc_server = None
            self._queue.put_nowait(None)

    async def events(self) -> AsyncIterator[dict[str, object]]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def _pump_stdout(self, stream: asyncio.StreamReader) -> None:
        try:
            async for line in _iter_lines(stream, max_frame_bytes=self._max_frame_bytes):
                if not line.strip():
                    continue
                event = object_of(json.loads, line)
                self._route(event)
        except (ValueError, TransportClosedError, OSError) as exc:
            self._stderr.append(f"invalid ACP frame: {exc}")
        finally:
            self._stream_closed = True
            for future in self._setup.values():
                if not future.done():
                    future.set_exception(TransportClosedError("agy ACP stream ended"))
            self._queue.put_nowait(None)
        await _drain_pipe(stream)

    def _route(self, event: object) -> None:
        if not is_object_dict(event) or event.get("jsonrpc") != "2.0":
            raise ValueError("agy stdout contains a frame outside JSON-RPC 2.0")
        request_id = event.get("id")
        method = event.get("method")
        if method is None and type(request_id) is int:
            setup = self._setup.get(request_id)
            if setup is not None:
                if not setup.done():
                    if request_id == self._live_request and "result" in event:
                        self._session_ready = True
                    setup.set_result(event)
                return
        if self._session_ready:
            self._queue.put_nowait(event)
        elif method is not None and isinstance(request_id, (int, str)):
            self.respond_error(request_id)

    async def _pump_stderr(self, stream: asyncio.StreamReader) -> None:
        with contextlib.suppress(ValueError):
            async for line in _iter_lines(stream, max_frame_bytes=self._max_frame_bytes):
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    self._stderr.append(text)
        await _drain_pipe(stream)

    def _require_process(self) -> asyncio.subprocess.Process:
        if self._process is None:
            raise TransportClosedError("agy transport is not started")
        return self._process
