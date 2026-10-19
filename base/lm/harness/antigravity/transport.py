from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import time
from collections import deque
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, cast

from base.lm.claude_sdk.process import (
    ChildProbe,
    ProcessObservation,
    PsutilChildProbe,
    describe,
    snapshot,
    wait_gone,
)
from base.lm.harness.antigravity.config import AntigravityConfig

# A single NDJSON line can carry a whole tool result; the 64 KiB StreamReader
# default would truncate the stream instead of the line.
READER_LIMIT_BYTES = 8 * 1024 * 1024
CANCEL_POLL_SECONDS = 0.02
STDERR_TAIL_LINES = 40
STREAM_END = object()


class TransportClosedError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class InterruptReport:
    turn_id: str | None
    interrupt_ms: float
    settled: bool | None
    observed: tuple[ProcessObservation, ...]
    remaining: tuple[ProcessObservation, ...]
    waited_ms: float
    cli_pid: int | None
    cli_alive: bool | None


# meiva:note
# `agy --input-format stream-json` keeps one process for the whole session: stdin
# carries `{"event":"user","message":{"content":…}}` lines, stdout carries
# `init` / `step_update` / `result`. It is not JSON-RPC — no ids, no methods — so
# a cancel cannot be a request: SIGINT is the only interrupt, and the CLI answers
# it by emitting a final `result` and exiting 1. `interrupt()` therefore also
# ends the process, and the session must respawn with `--conversation` to keep
# serving turns.
# meiva:link base/lm/harness/antigravity/docs/antigravity.meiva.okf.md
class AgyTransport:
    def __init__(
        self,
        config: AntigravityConfig,
        *,
        environ: Mapping[str, str] | None = None,
        probe: ChildProbe | None = None,
    ) -> None:
        self._config = config
        self._environ = dict(environ) if environ else None
        self._probe: ChildProbe = probe or PsutilChildProbe()
        self._process: asyncio.subprocess.Process | None = None
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._tasks = asyncio.TaskGroup()
        self._tasks_entered = False
        self._pumps: list[asyncio.Task[None]] = []
        self._stderr: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        self._conversation_id: str | None = None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def conversation_id(self) -> str | None:
        return self._conversation_id

    @property
    def stderr_tail(self) -> tuple[str, ...]:
        return tuple(self._stderr)

    async def start(self, *, resume: str | None) -> None:
        if self._process is not None:
            raise RuntimeError("transport already started")
        argv = self._config.command(resume=resume)
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(self._config.cwd),
            env=self._environ,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=READER_LIMIT_BYTES,
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

    async def interrupt(
        self,
        *,
        turn_id: str | None = None,
        baseline: Mapping[int, str] | None = None,
    ) -> InterruptReport:
        process = self._process
        if process is None or process.returncode is not None:
            return InterruptReport(
                turn_id=turn_id,
                interrupt_ms=0.0,
                settled=None,
                observed=(),
                remaining=(),
                waited_ms=0.0,
                cli_pid=None,
                cli_alive=None,
            )
        pid = process.pid
        observed = snapshot(self._probe, pid)
        spawned = {
            child: command
            for child, command in observed.items()
            if baseline is None or child not in baseline
        }
        started = time.perf_counter()
        process.send_signal(signal.SIGINT)
        await self._settle_process(process)
        interrupt_ms = (time.perf_counter() - started) * 1000.0
        gone, waited_ms = await wait_gone(
            self._probe,
            spawned,
            timeout_seconds=self._config.cancel_settle_seconds,
            poll_seconds=CANCEL_POLL_SECONDS,
        )
        remaining = (
            ()
            if gone
            else describe(observed, [child for child in spawned if self._probe.is_alive(child)])
        )
        return InterruptReport(
            turn_id=turn_id,
            interrupt_ms=interrupt_ms,
            settled=gone,
            observed=describe(observed, spawned),
            remaining=remaining,
            waited_ms=waited_ms,
            cli_pid=pid,
            cli_alive=self._probe.is_alive(pid),
        )

    async def close(self) -> None:
        process = self._process
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=self._config.cancel_settle_seconds)
            except TimeoutError:
                process.kill()
                await process.wait()
        for pump in self._pumps:
            pump.cancel()
        self._pumps = []
        if self._tasks_entered:
            self._tasks_entered = False
            with contextlib.suppress(ExceptionGroup):
                await self._tasks.__aexit__(None, None, None)
        self._process = None
        await self._queue.put(STREAM_END)

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            item = await self._queue.get()
            if item is STREAM_END:
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
            while True:
                try:
                    line = await stream.readline()
                except ValueError:
                    self._stderr.append("stdout line exceeded the reader limit")
                    break
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    self._stderr.append(f"unparsed stdout: {text[:200]}")
                    continue
                if not isinstance(payload, dict):
                    continue
                event = cast("dict[str, Any]", payload)
                if event.get("event") == "init":
                    session_id = event.get("conversation_id")
                    if isinstance(session_id, str):
                        self._conversation_id = session_id
                await self._queue.put(event)
        finally:
            await self._queue.put(STREAM_END)

    async def _pump_stderr(self, stream: asyncio.StreamReader) -> None:
        while True:
            try:
                line = await stream.readline()
            except ValueError:
                break
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                self._stderr.append(text)

    def _require_process(self) -> asyncio.subprocess.Process:
        if self._process is None:
            raise TransportClosedError("agy transport is not started")
        return self._process
