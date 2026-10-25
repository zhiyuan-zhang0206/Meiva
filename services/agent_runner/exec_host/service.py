# meiva:note
# The exec-host service: runs one disposable `agent.execution.child` process group
# per `run` connection over `exec-host.sock`, streams stdout/stderr chunks, and
# enforces process-group cleanup on exit, timeout, cancel, or client disconnect.

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeGuard

import psutil

from base.log import logger
from base.native_process.exec_domain import KILL_GRACE_S, ExecProcessDomain
from base.native_process.process_control import SYSTEM_PROCESS_CONTROL
from base.paths import exec_run_dir
from base.sessions.exec_host import protocol
from base.sessions.exec_host.child_environment import ChildExecution, build_child_environment

_POLL_INTERVAL_S = 0.05
_OUTPUT_DRAIN_TIMEOUT_S = 5.0
_REQUEST_READ_TIMEOUT_S = 30.0
_LISTEN_BACKLOG = 128


class RequestError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class _RunRequest:
    req_id: int | str | None
    agent_id: int | None
    request_path: Path
    result_path: Path
    timeout_s: float
    cwd: Path
    env_overlay: tuple[tuple[str, str], ...]
    config_overlay_json: str | None
    birth_config_json: str | None


class _OutputPipe:
    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        if proc.stdout is None:
            raise RuntimeError("exec child has no output pipe")
        self.stdout = proc.stdout
        self.descriptor = self.stdout.fileno()
        self.chunks: list[str] = []
        self.closed = False
        self.loop: asyncio.AbstractEventLoop | None = None
        os.set_blocking(self.descriptor, False)

    def watch(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.loop.add_reader(self.descriptor, self.pump)

    def pump(self) -> None:
        if self.closed:
            return
        try:
            for _ in range(4):
                chunk = os.read(self.descriptor, 65536)
                if not chunk:
                    self.close()
                    return
                self.chunks.append(chunk.decode("utf-8", errors="replace"))
        except BlockingIOError:
            return
        except (OSError, ValueError):
            self.close()

    def take_pending(self) -> str:
        if not self.chunks:
            return ""
        text = "".join(self.chunks)
        self.chunks.clear()
        return text

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            if self.loop is not None:
                self.loop.remove_reader(self.descriptor)
            self.stdout.close()

    async def finish(self, timeout_s: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        self.pump()
        while not self.closed:
            self.pump()
            now = loop.time()
            if self.closed or now >= deadline:
                return
            await asyncio.sleep(min(0.01, max(0.0, deadline - now)))


def _validate_envelope_path(raw: object, key: str, allowed_root: Path) -> Path:
    if not isinstance(raw, str) or not raw:
        raise RequestError(protocol.BAD_REQUEST, f"{key} must be a non-empty string")
    resolved = Path(raw).resolve()
    if not resolved.is_relative_to(allowed_root):
        raise RequestError(
            protocol.BAD_REQUEST,
            f"{key} must be inside {allowed_root}: {raw}",
        )
    return resolved


def _is_object_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _validate_env_overlay(raw: object) -> tuple[tuple[str, str], ...]:
    if raw is None:
        return ()
    if not _is_object_dict(raw):
        raise RequestError(protocol.BAD_REQUEST, "env_overlay must be an object of strings")
    result: list[tuple[str, str]] = []
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise RequestError(protocol.BAD_REQUEST, "env_overlay must be an object of strings")
        if not key or "=" in key or "\0" in key or "\0" in value:
            raise RequestError(
                protocol.BAD_REQUEST, f"env_overlay entry {key!r} cannot be forwarded"
            )
        result.append((key, value))
    return tuple(result)


def _validate_optional_map_json(raw: object, key: str) -> str | None:
    if raw is None:
        return None
    if not _is_object_dict(raw):
        raise RequestError(protocol.BAD_REQUEST, f"{key} must be a JSON object or null")
    if not raw:
        return None
    for k in raw:
        if not isinstance(k, str):
            raise RequestError(protocol.BAD_REQUEST, f"{key} keys must be strings")
    return json.dumps(raw, sort_keys=True)


def _coerce_req_id(raw: object) -> int | str | None:
    if raw is None or isinstance(raw, str):
        return raw
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    return str(raw)


def _parse_run_request(req: dict[str, Any], *, home: Path, default_cwd: Path) -> _RunRequest:
    req_id = _coerce_req_id(req.get("id"))
    raw_agent_id = req.get("agent_id")
    if raw_agent_id is not None and (
        isinstance(raw_agent_id, bool) or not isinstance(raw_agent_id, int)
    ):
        raise RequestError(protocol.BAD_REQUEST, "agent_id must be an integer or null")

    allowed_root = exec_run_dir(home).resolve()
    request_path = _validate_envelope_path(req.get("request_path"), "request_path", allowed_root)
    result_path = _validate_envelope_path(req.get("result_path"), "result_path", allowed_root)

    raw_timeout = req.get("timeout_s")
    if (
        isinstance(raw_timeout, bool)
        or not isinstance(raw_timeout, int | float)
        or raw_timeout <= 0
    ):
        raise RequestError(protocol.BAD_REQUEST, "timeout_s must be a positive number")

    raw_cwd = req.get("cwd")
    if raw_cwd is None:
        cwd = default_cwd
    elif isinstance(raw_cwd, str) and raw_cwd:
        cwd = Path(raw_cwd)
    else:
        raise RequestError(protocol.BAD_REQUEST, "cwd must be a non-empty string or null")
    if not cwd.is_dir():
        raise RequestError(protocol.BAD_REQUEST, f"cwd is not a directory: {cwd}")

    return _RunRequest(
        req_id=req_id,
        agent_id=raw_agent_id,
        request_path=request_path,
        result_path=result_path,
        timeout_s=float(raw_timeout),
        cwd=cwd,
        env_overlay=_validate_env_overlay(req.get("env_overlay")),
        config_overlay_json=_validate_optional_map_json(
            req.get("config_overlay"), "config_overlay"
        ),
        birth_config_json=_validate_optional_map_json(req.get("birth_config"), "birth_config"),
    )


def _build_child_env(
    base_environ: Mapping[str, str], parsed: _RunRequest, source_root: Path | None
) -> dict[str, str]:
    env = dict(base_environ)
    helper_pid = base_environ.get("MEIVA_PERMISSIONS_HELPER_PID")
    env.update(parsed.env_overlay)
    if helper_pid is not None:
        env["MEIVA_PERMISSIONS_HELPER_PID"] = helper_pid
    return build_child_environment(
        env,
        cwd=parsed.cwd,
        source_root=source_root,
        execution=ChildExecution(parsed.agent_id, parsed.request_path, parsed.result_path),
        config_overlay_json=parsed.config_overlay_json,
        birth_config_json=parsed.birth_config_json,
    )


def _leader_exited(leader: psutil.Process) -> bool:
    try:
        return leader.status() in {psutil.STATUS_DEAD, psutil.STATUS_ZOMBIE}
    except psutil.NoSuchProcess:
        return True


async def _write_message(writer: asyncio.StreamWriter, message: dict[str, Any]) -> bool:
    try:
        writer.write(protocol.encode(message))
        await writer.drain()
        return True
    except (OSError, ConnectionError):
        return False


async def _wait_client_control(
    reader: asyncio.StreamReader,
    cancel_event: asyncio.Event,
    timeout_s: float,
) -> bool:
    if cancel_event.is_set():
        return False
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=timeout_s)
    except TimeoutError:
        return False
    except (OSError, ValueError):
        cancel_event.set()
        return True
    if not line:
        cancel_event.set()
        return True
    with contextlib.suppress(ValueError, TypeError, UnicodeDecodeError):
        msg = protocol.decode_object(line)
        if msg.get("method") == "cancel":
            cancel_event.set()
    return False


async def _await_leader_exit(domain: ExecProcessDomain, leader: psutil.Process) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + KILL_GRACE_S
    while not _leader_exited(leader):
        if loop.time() >= deadline:
            logger.warning(
                "[{label}] exec child {pid} survived the {grace}s grace period — "
                "hard-stopping its process group",
                label="exec-subprocess-killed",
                pid=domain.proc.pid,
                grace=KILL_GRACE_S,
                event="exec_subprocess_killed",
            )
            return
        await asyncio.sleep(_POLL_INTERVAL_S)


def _close_domain(domain: ExecProcessDomain) -> None:
    stdin = domain.proc.stdin
    try:
        domain.kill_and_reap()
    finally:
        if stdin is not None:
            stdin.close()


def _close_output(pipe: _OutputPipe) -> None:
    if not pipe.closed:
        logger.warning(
            "exec output pipe stayed open after its process group was killed; "
            "a descendant outside the group holds it, so the rest of its output is dropped"
        )
        pipe.close()


class ExecHostService:
    def __init__(
        self,
        *,
        home: Path,
        base_environ: Mapping[str, str],
        default_cwd: Path,
        source_root: Path | None,
        executable: str = sys.executable,
        pid: int = 0,
    ) -> None:
        self._home = home
        self._executable = executable
        self._base_environ = dict(base_environ)
        self._default_cwd = default_cwd
        self._source_root = source_root
        self._pid = pid
        self.stop_event = asyncio.Event()
        self._closing = False
        self._active_runs: set[asyncio.Event] = set()
        self._runs_drained = asyncio.Event()
        self._runs_drained.set()

    def _spawn_child(
        self, parsed: _RunRequest
    ) -> tuple[subprocess.Popen[bytes], ExecProcessDomain]:
        argv = [self._executable, "-I", "-B", "-X", "utf8", "-m", "agent.execution.child"]
        env = _build_child_env(self._base_environ, parsed, self._source_root)
        return ExecProcessDomain.launch_posix(
            argv,
            process_control=SYSTEM_PROCESS_CONTROL,
            cwd=parsed.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
        )

    async def _poll_run(
        self,
        domain: ExecProcessDomain,
        leader: psutil.Process,
        pipe: _OutputPipe,
        parsed: _RunRequest,
        io_pair: tuple[asyncio.StreamReader, asyncio.StreamWriter],
        cancel_event: asyncio.Event,
    ) -> tuple[bool, bool, bool]:
        reader, writer = io_pair
        loop = asyncio.get_running_loop()
        deadline = loop.time() + parsed.timeout_s
        disconnected = False
        while not _leader_exited(leader):
            pipe.pump()
            pending = pipe.take_pending()
            if (
                pending
                and not disconnected
                and not await _write_message(writer, protocol.output_chunk(parsed.req_id, pending))
            ):
                disconnected = True
                cancel_event.set()
            if cancel_event.is_set():
                domain.signal(signal.SIGINT)
                return True, False, disconnected
            if loop.time() >= deadline:
                domain.signal(signal.SIGTERM)
                return False, True, disconnected
            if await _wait_client_control(reader, cancel_event, _POLL_INTERVAL_S):
                disconnected = True
            if cancel_event.is_set():
                domain.signal(signal.SIGINT)
                return True, False, disconnected
        return False, False, disconnected

    async def _execute_run(
        self,
        parsed: _RunRequest,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        cancel_event: asyncio.Event,
    ) -> None:
        domain: ExecProcessDomain | None = None
        pipe: _OutputPipe | None = None
        try:
            try:
                proc, domain = self._spawn_child(parsed)
            except OSError as exc:
                await _write_message(
                    writer,
                    protocol.err(
                        parsed.req_id,
                        protocol.ERROR,
                        f"exec subprocess could not be spawned: {exc}",
                    ),
                )
                return

            leader = psutil.Process(proc.pid)
            pipe = _OutputPipe(proc)
            pipe.watch()

            cancelled, timed_out, disconnected = await self._poll_run(
                domain, leader, pipe, parsed, (reader, writer), cancel_event
            )
            if cancelled or timed_out:
                await _await_leader_exit(domain, leader)
            _close_domain(domain)
            await pipe.finish(_OUTPUT_DRAIN_TIMEOUT_S)
            _close_output(pipe)

            pending = pipe.take_pending()
            if pending and not disconnected:
                disconnected = not await _write_message(
                    writer, protocol.output_chunk(parsed.req_id, pending)
                )
            if not disconnected:
                await _write_message(
                    writer,
                    protocol.ok(
                        parsed.req_id,
                        {
                            "returncode": proc.returncode,
                            "cancelled": cancelled,
                            "timed_out": timed_out,
                        },
                    ),
                )
        finally:
            if domain is not None:
                _close_domain(domain)
            if pipe is not None:
                _close_output(pipe)

    async def _handle_run(
        self,
        req: dict[str, Any],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        req_id = req.get("id")
        if self._closing:
            await _write_message(
                writer, protocol.err(req_id, protocol.ERROR, "exec-host is shutting down")
            )
            return
        try:
            parsed = _parse_run_request(req, home=self._home, default_cwd=self._default_cwd)
        except RequestError as exc:
            await _write_message(writer, protocol.err(req_id, exc.code, exc.message))
            return

        cancel_event = asyncio.Event()
        self._active_runs.add(cancel_event)
        self._runs_drained.clear()
        try:
            await self._execute_run(parsed, reader, writer, cancel_event)
        finally:
            self._active_runs.discard(cancel_event)
            if not self._active_runs:
                self._runs_drained.set()

    async def _connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                line = await asyncio.wait_for(reader.readline(), _REQUEST_READ_TIMEOUT_S)
                if not line:
                    return
                req = protocol.decode_object(line)
            except (ValueError, TypeError, UnicodeDecodeError, TimeoutError, OSError) as exc:
                await _write_message(
                    writer, protocol.err(None, protocol.ERROR, f"bad request: {exc}")
                )
                return

            req_id = req.get("id")
            method = req.get("method")
            if method == "ping":
                await _write_message(writer, protocol.ok(req_id, {"pid": self._pid}))
            elif method == "run":
                await self._handle_run(req, reader, writer)
            else:
                await _write_message(
                    writer,
                    protocol.err(req_id, protocol.BAD_REQUEST, f"unknown method {method!r}"),
                )
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    def request_stop(self, signum: int | None = None) -> None:
        if signum is not None:
            logger.info("exec-host stopping on signal {signum}", signum=signum)
        self._closing = True
        for ev in list(self._active_runs):
            ev.set()
        self.stop_event.set()

    async def serve(
        self,
        path: Path,
        *,
        install_signals: bool = True,
        drain_timeout_s: float = 5.0,
    ) -> None:
        loop = asyncio.get_running_loop()
        if install_signals:
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, self.request_stop, sig)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(
            self._connection,
            path=str(path),
            limit=protocol.MAX_REQUEST_BYTES,
            backlog=_LISTEN_BACKLOG,
        )
        path.chmod(0o600)
        bound = path.stat().st_ino
        logger.info("exec-host serving {path}", path=path)
        await self.stop_event.wait()
        self.request_stop()
        server.close()
        with contextlib.suppress(OSError):
            if path.stat().st_ino == bound:
                path.unlink()
        if self._active_runs:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._runs_drained.wait(), timeout=drain_timeout_s)
