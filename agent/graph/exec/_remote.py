# meiva:note
# Client for the per-home `exec-host` unix-socket service (`<home>/run/exec-host.sock`).
#
# When `exec-host.sock` is listening in the home's run directory (`exec_dir.parent`),
# `run_via_exec_host` delegates child launch and process-group supervision to `exec-host`,
# streaming output frames into `StreamingTextIO` / `ExecOutputChunkPublisher`, forwarding
# cancellation in-band, and returning `(returncode, cancelled, timed_out)` so
# `run_in_subprocess` reads the result envelope identically to a local child. When no
# `exec-host` listener is present, `run_via_exec_host` returns `None` and `run_in_subprocess`
# falls back to spawning `agent.execution.child` directly.

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any, TypeGuard

from base.clock import Timers
from base.sessions.exec_host import protocol
from base.sessions.exec_host.paths import service_socket_path

from ._child_boundary import ParentFacts
from ._poll import Stop, plan_poll
from ._result import ExecCrashed
from ._stream import ExecOutputChunkPublisher, StreamingTextIO

_POLL_INTERVAL_S = 0.05


# meiva:note
# Given `exec_dir` (`<home>/run/exec`), `exec_dir.parent` is `<home>/run`, where `exec-host.sock`
# (or its sun_path-bounded `/tmp/meiva-exec-<uid>/<digest>.sock` fallback) is located.
def exec_host_socket(exec_dir: Path) -> Path:
    return service_socket_path(exec_dir.parent)


async def _open_exec_host(
    sock_path: Path,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
    if not sock_path.exists():
        return None
    try:
        return await asyncio.open_unix_connection(str(sock_path), limit=protocol.MAX_REQUEST_BYTES)
    except OSError:
        return None


def _spawn_error(message: dict[str, Any]) -> ExecCrashed:
    raw_error = str(message.get("error") or "unknown exec-host error")
    prefix = "exec subprocess could not be spawned: "
    output = raw_error if raw_error.startswith(prefix) else f"{prefix}{raw_error}"
    return ExecCrashed(output=output, exc=OSError(raw_error))


def _is_str_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def _completion_from_response(
    message: dict[str, Any],
    stream: StreamingTextIO,
    chunk_publisher: ExecOutputChunkPublisher | None,
) -> tuple[int | None, bool, bool] | ExecCrashed:
    if not message["ok"]:
        return _spawn_error(message)
    if chunk_publisher is not None:
        pending = stream.take_pending()
        if pending:
            chunk_publisher.publish(pending)
    raw_data = message.get("data")
    if not _is_str_object_dict(raw_data):
        return None, False, False
    raw_rc = raw_data.get("returncode")
    returncode = raw_rc if isinstance(raw_rc, int) and not isinstance(raw_rc, bool) else None
    return returncode, bool(raw_data.get("cancelled")), bool(raw_data.get("timed_out"))


async def _read_next_frame(reader: asyncio.StreamReader, timeout_s: float) -> dict[str, Any] | None:
    try:
        raw = await asyncio.wait_for(reader.readline(), timeout=timeout_s)
    except TimeoutError:
        return None
    if not raw:
        raise EOFError("exec-host closed connection before sending final response")
    return protocol.decode_object(raw.rstrip(b"\r\n"))


async def _send_cancel(writer: asyncio.StreamWriter, req_id: int) -> bool:
    with contextlib.suppress(OSError):
        writer.write(protocol.encode({"id": req_id, "method": "cancel"}))
        await writer.drain()
    return True


def _apply_poll_beat(
    stream: StreamingTextIO,
    chunk_publisher: ExecOutputChunkPublisher | None,
    *,
    now: float,
    last_activity: float,
    deadline: float,
    cancel_requested: bool,
) -> tuple[float, bool]:
    pending = stream.take_pending() if chunk_publisher is not None else ""
    plan = plan_poll(
        pending=bool(pending),
        now=now,
        last_activity=last_activity,
        deadline=deadline,
        cancel_requested=cancel_requested,
    )
    if chunk_publisher is not None and plan.forward:
        chunk_publisher.publish(pending)
        last_activity = now
    elif chunk_publisher is not None and plan.keepalive:
        chunk_publisher.keepalive()
        last_activity = now
    return last_activity, plan.stop is Stop.CANCEL


async def _stream_exec_host(
    req_id: int,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    stream: StreamingTextIO,
    chunk_publisher: ExecOutputChunkPublisher | None,
    cancel_event: asyncio.Event,
    timeout: float,
    timers: Timers,
) -> tuple[int | None, bool, bool] | ExecCrashed:
    started = timers.monotonic()
    deadline = started + timeout
    last_activity = started
    cancel_sent = False
    while True:
        if cancel_event.is_set() and not cancel_sent:
            cancel_sent = await _send_cancel(writer, req_id)
        try:
            frame = await _read_next_frame(reader, _POLL_INTERVAL_S)
        except (EOFError, OSError, ValueError, TypeError) as exc:
            return ExecCrashed(
                output=f"exec subprocess could not be spawned: {exc}",
                exc=exc,
            )
        if frame is not None and "ok" in frame:
            return _completion_from_response(frame, stream, chunk_publisher)
        if frame is not None and frame.get("type") == "output":
            data = frame.get("data")
            if isinstance(data, str) and data:
                stream.write(data)
        last_activity, should_cancel = _apply_poll_beat(
            stream,
            chunk_publisher,
            now=timers.monotonic(),
            last_activity=last_activity,
            deadline=deadline,
            cancel_requested=cancel_event.is_set(),
        )
        if should_cancel and not cancel_sent:
            cancel_sent = await _send_cancel(writer, req_id)


# meiva:note
# Run one `execute_code` child through the home's `exec-host` service when `exec-host.sock`
# is listening under `exec_dir.parent`. Returns `None` when no `exec-host` service is reachable
# so `run_in_subprocess` falls back to direct local child spawning.
async def run_via_exec_host(
    *,
    exec_dir: Path,
    parent: ParentFacts,
    agent_id: int | None,
    request_path: Path,
    result_path: Path,
    timeout: float,
    stream: StreamingTextIO,
    chunk_publisher: ExecOutputChunkPublisher | None,
    cancel_event: asyncio.Event,
    config_overlay: dict[str, object] | None,
    birth_config: dict[str, object] | None,
    timers: Timers,
) -> tuple[int | None, bool, bool] | ExecCrashed | None:
    connection = await _open_exec_host(exec_host_socket(exec_dir))
    if connection is None:
        return None
    reader, writer = connection
    req_id = 1
    request: dict[str, Any] = {
        "id": req_id,
        "method": "run",
        "agent_id": agent_id,
        "request_path": str(request_path),
        "result_path": str(result_path),
        "timeout_s": timeout,
        "config_overlay": config_overlay,
        "birth_config": birth_config,
        "cwd": str(parent.cwd),
        "env_overlay": dict(parent.environ),
    }
    try:
        try:
            writer.write(protocol.encode(request))
            await writer.drain()
        except OSError as exc:
            return ExecCrashed(
                output=f"exec subprocess could not be spawned: {exc}",
                exc=exc,
            )
        return await _stream_exec_host(
            req_id,
            reader,
            writer,
            stream,
            chunk_publisher,
            cancel_event,
            timeout,
            timers,
        )
    finally:
        writer.close()


__all__ = ["exec_host_socket", "run_via_exec_host"]
