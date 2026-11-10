from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import signal
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable, Mapping

import psutil

from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of
from base.lm.harness.antigravity.config import AntigravityConfig
from base.lm.harness.antigravity.rpc import (
    LIVE_SUBMIT_TIMEOUT_SECONDS,
    AntigravityServer,
    discover_process_antigravity_server,
    send_user_cascade_message,
)
from base.lm.harness.cancel import (
    CancelReport,
    Sweep,
    observed_cancel,
    spawned_since,
    unobserved_cancel,
)
from base.lm.harness.process import ChildProbe, PsutilChildProbe, WaitClock, snapshot, wait_gone

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


class TransportClosedError(RuntimeError):
    pass


Reaper = Callable[[Mapping[int, str], "Iterable[int]"], "tuple[int, ...]"]


# meiva:note
# The CLI exits on SIGINT but its tool tree does not: `run_command` children are
# orphaned, and nobody outside this call knows their pids. The adapter therefore
# reaps what it observed — SIGKILLing the observed pids and skipping any whose
# command line no longer matches, so a reused pid is not killed by mistake — and
# still reports honestly: `settled` is true only when every observed pid is
# gone, so a process that survives SIGKILL shows up in `remaining` instead of
# being rounded up to a clean cancel.
# meiva:link base/lm/harness/antigravity/docs/antigravity.md
def kill_orphans(observed: Mapping[int, str], pids: Iterable[int]) -> tuple[int, ...]:
    killed: list[int] = []
    for pid in pids:
        if pid <= 1:
            continue
        try:
            process = psutil.Process(pid)
            command = " ".join(process.cmdline())[:200]
        except psutil.Error:
            continue
        if observed.get(pid) and command != observed[pid]:
            continue
        try:
            process.kill()
        except psutil.Error:
            continue
        killed.append(pid)
    return tuple(killed)


# meiva:note
# `agy --input-format stream-json` keeps one process for the whole session: stdin
# carries `{"event":"user","message":{"content":…}}` lines, stdout carries
# `init` / `step_update` / `result`. It is not JSON-RPC — no ids, no methods — so
# a cancel cannot be a request: SIGINT is the only interrupt, and the CLI answers
# it by emitting a final `result` and exiting 1. `interrupt()` therefore also
# ends the process, and the session must respawn with `--conversation` to keep
# serving turns.
# meiva:link base/lm/harness/antigravity/docs/antigravity.md
class AgyTransport:
    def __init__(
        self,
        config: AntigravityConfig,
        *,
        environ: Mapping[str, str] | None = None,
        probe: ChildProbe | None = None,
        reap: Reaper | None = None,
        clock: WaitClock,
        max_frame_bytes: int = MAX_FRAME_BYTES,
    ) -> None:
        self._config = config
        self._clock = clock
        self._max_frame_bytes = max_frame_bytes
        self._reap: Reaper = reap or kill_orphans
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
        self._probe: ChildProbe = probe or PsutilChildProbe()
        self._process: asyncio.subprocess.Process | None = None
        self._returncode: int | None = None
        self._queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()
        self._tasks = asyncio.TaskGroup()
        self._tasks_entered = False
        self._pumps: list[asyncio.Task[None]] = []
        self._stderr: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        self._conversation_id: str | None = None
        self._init_ready = asyncio.Event()
        self._rpc_server: AntigravityServer | None = None

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

    async def start(self, *, resume: str | None) -> None:
        if self._process is not None:
            raise RuntimeError("transport already started")
        if resume:
            self._conversation_id = resume
            self._init_ready.set()
        argv = self._config.command(resume=resume)
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(self._config.cwd),
            env=self._environ,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=READ_CHUNK_BYTES,
            start_new_session=True,
        )
        self._process = process
        if process.stdout is None or process.stderr is None:
            raise RuntimeError("agy stdio pipes are not available")
        if not self._tasks_entered:
            await self._tasks.__aenter__()
            self._tasks_entered = True
        self._pumps = [
            self._tasks.create_task(self._pump_stdout(process.stdout)),
            self._tasks.create_task(self._pump_stderr(process.stderr)),
        ]

    async def send(self, text: str) -> None:
        process = self._require_process()
        if process.stdin is None or process.returncode is not None:
            raise TransportClosedError("agy is not running")
        line = json.dumps({"event": "user", "message": {"content": text}}) + "\n"
        try:
            process.stdin.write(line.encode("utf-8"))
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise TransportClosedError(f"agy stdin closed: {exc}") from exc

    # meiva:note
    # `agy` printmode blocks stdin while `pollResponse` waits on `IsCascadeRunning`,
    # so mid-turn steering cannot ride on stdin. Instead, `agy` starts its in-process
    # Connect JSON-RPC `LanguageServerService` on localhost and reads
    # `ANTIGRAVITY_SERVER_CSRF_TOKEN` at startup. Calling `SendUserCascadeMessage`
    # queues a `CORTEX_STEP_TYPE_USER_INPUT` step directly into the running
    # `AgentExecutor` loop (`messages.Hub`), which `pollResponse` streams out as part
    # of the ongoing turn.
    # meiva:link base/lm/harness/antigravity/docs/antigravity.md
    async def steer(self, text: str, *, conversation_id: str | None = None) -> None:
        process = self._require_process()
        if process.returncode is not None:
            raise TransportClosedError("agy is not running")
        target_conversation = conversation_id or self._conversation_id
        if not target_conversation:
            try:
                await asyncio.wait_for(self._init_ready.wait(), timeout=LIVE_SUBMIT_TIMEOUT_SECONDS)
            except TimeoutError as exc:
                raise TransportClosedError("agy conversation id was not initialized") from exc
            target_conversation = conversation_id or self._conversation_id
        if process.returncode is not None or not target_conversation:
            raise TransportClosedError("agy is not running")
        server = self._rpc_server
        if server is None:
            server = await self._discover_rpc_server(process.pid)
            self._rpc_server = server
        _ = await asyncio.to_thread(
            send_user_cascade_message,
            target_conversation,
            text,
            server=server,
        )

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

    async def interrupt(
        self,
        *,
        turn_id: str | None = None,
        baseline: Mapping[int, str] | None = None,
    ) -> CancelReport:
        process = self._process
        if process is None or process.returncode is not None:
            return unobserved_cancel(turn_id=turn_id)
        pid = process.pid
        observed = snapshot(self._probe, pid)
        spawned = spawned_since(observed, {} if baseline is None else baseline)
        started = self._clock.monotonic()
        process.send_signal(signal.SIGINT)
        await self._settle_process(process)
        interrupt_ms = (self._clock.monotonic() - started) * 1000.0
        gone, waited_ms = await wait_gone(
            self._probe,
            spawned,
            timeout_seconds=self._config.cancel_settle_seconds,
            poll_seconds=CANCEL_POLL_SECONDS,
            clock=self._clock,
        )
        killed: tuple[int, ...] = ()
        if not gone:
            alive = tuple(child for child in spawned if self._probe.is_alive(child))
            killed = self._reap(observed, alive)
            if killed:
                reaped, extra_ms = await wait_gone(
                    self._probe,
                    killed,
                    timeout_seconds=self._config.cancel_settle_seconds,
                    poll_seconds=CANCEL_POLL_SECONDS,
                    clock=self._clock,
                )
                waited_ms += extra_ms
                gone = reaped
        return observed_cancel(
            turn_id=turn_id,
            interrupt_ms=interrupt_ms,
            pid=pid,
            cli_alive=self._probe.is_alive(pid),
            sweep=Sweep(
                observed=observed,
                spawned=spawned,
                gone=gone,
                waited_ms=waited_ms,
                alive=() if gone else [child for child in spawned if self._probe.is_alive(child)],
            ),
            killed=killed,
        )

    async def close(self) -> None:
        process = self._process
        if process is not None and process.returncode is None:
            await self.interrupt()
        for pump in self._pumps:
            pump.cancel()
        self._pumps = []
        if self._tasks_entered:
            self._tasks_entered = False
            with contextlib.suppress(ExceptionGroup):
                await self._tasks.__aexit__(None, None, None)
        if process is not None:
            self._returncode = process.returncode
        self._process = None
        self._rpc_server = None
        self._init_ready.set()
        await self._queue.put(None)

    async def events(self) -> AsyncIterator[dict[str, object]]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def _settle_process(self, process: asyncio.subprocess.Process) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=self._config.cancel_settle_seconds)
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=self._config.cancel_settle_seconds)
        except TimeoutError:
            process.kill()
            await process.wait()

    async def _pump_stdout(self, stream: asyncio.StreamReader) -> None:
        try:
            try:
                async for line in _iter_lines(stream, max_frame_bytes=self._max_frame_bytes):
                    text = line.decode("utf-8", "replace").strip()
                    if not text:
                        continue
                    try:
                        event = object_of(json.loads, text)
                    except json.JSONDecodeError:
                        self._stderr.append(f"unparsed stdout: {text[:200]}")
                        continue
                    if not is_object_dict(event):
                        continue
                    if event.get("event") == "init":
                        session_id = event.get("conversation_id")
                        if isinstance(session_id, str) and session_id:
                            self._conversation_id = session_id
                            self._init_ready.set()
                    await self._queue.put(event)
            except ValueError:
                self._stderr.append("stdout line exceeded the reader limit")
        finally:
            self._init_ready.set()
            await self._queue.put(None)

    async def _pump_stderr(self, stream: asyncio.StreamReader) -> None:
        with contextlib.suppress(ValueError):
            async for line in _iter_lines(stream, max_frame_bytes=self._max_frame_bytes):
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    self._stderr.append(text)

    def _require_process(self) -> asyncio.subprocess.Process:
        if self._process is None:
            raise TransportClosedError("agy transport is not started")
        return self._process
