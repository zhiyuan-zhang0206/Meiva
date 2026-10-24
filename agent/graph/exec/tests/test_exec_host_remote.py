# meiva:note
# Tests for `agent/graph/exec/_remote.py` and `run_in_subprocess` delegation to `exec-host.sock`.

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from agent.execution.protocol import ResultPayload, read_request, write_result
from agent.graph.exec._remote import exec_host_socket
from agent.graph.exec._result import ExecCancelled, ExecCrashed, ExecDone, ExecTimedOut
from agent.graph.exec._stream import ExecOutputChunkPublisher
from agent.graph.exec._subprocess import run_in_subprocess
from agent.graph.exec.tests.process_inputs import real_parent
from base.events.live.tests.live_capture import LiveEvents
from base.sessions.exec_host import protocol
from meiva.tests.fixtures.pin_agent import exec_context

_AGENT_ID = 424242


@asynccontextmanager
async def _unix_exec_host_server(
    sock_path: Path,
    handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]],
):
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    server = await asyncio.start_unix_server(handler, path=str(sock_path))
    try:
        yield
    finally:
        server.close()
        await server.wait_closed()
        sock_path.unlink(missing_ok=True)


async def test_run_in_subprocess_routes_through_exec_host_socket_and_streams_output(
    tmp_path: Path, live_events: LiveEvents
) -> None:
    exec_dir = tmp_path / "run" / "exec"
    sock_path = exec_host_socket(exec_dir)
    received_requests: list[dict[str, Any]] = []
    seen_code: list[str] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await reader.readline()
            req = protocol.decode_object(raw.rstrip(b"\r\n"))
            received_requests.append(req)
            envelope = read_request(Path(req["request_path"]))
            seen_code.append(envelope.code)

            writer.write(protocol.encode(protocol.output_chunk(req["id"], "remote ")))
            await writer.drain()
            await asyncio.sleep(0.06)
            writer.write(protocol.encode(protocol.output_chunk(req["id"], "hello\n")))
            await writer.drain()

            write_result(
                Path(req["result_path"]),
                ResultPayload(
                    kind="done",
                    state_update={"custom_slot": "updated-by-remote"},
                    sdk_calls=[{"method": "files.read", "count": 1}],
                ),
            )
            writer.write(
                protocol.encode(
                    protocol.ok(
                        req["id"],
                        {"returncode": 0, "cancelled": False, "timed_out": False},
                    )
                )
            )
            await writer.drain()
        finally:
            writer.close()

    parent = real_parent()
    publisher = ExecOutputChunkPublisher(live_events.publisher, agent_id=_AGENT_ID, item_id="3.0")
    async with _unix_exec_host_server(sock_path, handle):
        result, payload = await run_in_subprocess(
            "print('remote hello')",
            exec_context(_AGENT_ID),
            asyncio.Event(),
            30.0,
            publisher,
            accumulation_max_chars=1_000_000,
            parent=parent,
            exec_dir=exec_dir,
            config_overlay={"model": "test"},
            birth_config={"seed": 1},
        )

    assert isinstance(result, ExecDone)
    assert result.output == "remote hello\n"
    assert payload is not None
    assert (payload.state_update, payload.sdk_calls, seen_code) == (
        {"custom_slot": "updated-by-remote"},
        [{"method": "files.read", "count": 1}],
        ["print('remote hello')"],
    )
    assert len(received_requests) == 1
    req = received_requests[0]
    assert (
        req["method"],
        req["agent_id"],
        req["timeout_s"],
        req["config_overlay"],
        req["birth_config"],
        req["cwd"],
    ) == (
        "run",
        _AGENT_ID,
        30.0,
        {"model": "test"},
        {"seed": 1},
        str(parent.cwd),
    )

    chunks = [event for event in await live_events.events() if not event["keepalive"]]
    assert "".join(event["content"] for event in chunks) == "remote hello\n"
    assert not list((exec_dir / str(_AGENT_ID)).iterdir())


async def test_run_in_subprocess_forwards_cancel_over_exec_host_socket(tmp_path: Path) -> None:
    exec_dir = tmp_path / "run" / "exec"
    sock_path = exec_host_socket(exec_dir)
    started = asyncio.Event()
    received_cancel: list[dict[str, Any]] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await reader.readline()
            req = protocol.decode_object(raw.rstrip(b"\r\n"))
            writer.write(protocol.encode(protocol.output_chunk(req["id"], "working...\n")))
            await writer.drain()
            started.set()

            cancel_raw = await asyncio.wait_for(reader.readline(), timeout=5.0)
            cancel_msg = protocol.decode_object(cancel_raw.rstrip(b"\r\n"))
            received_cancel.append(cancel_msg)

            write_result(
                Path(req["result_path"]),
                ResultPayload(
                    kind="cancelled",
                    exc_type="KeyboardInterrupt",
                    exc_msg="",
                ),
            )
            writer.write(
                protocol.encode(
                    protocol.ok(
                        req["id"],
                        {"returncode": -2, "cancelled": True, "timed_out": False},
                    )
                )
            )
            await writer.drain()
        finally:
            writer.close()

    cancel_event = asyncio.Event()

    async def trigger_cancel() -> None:
        await started.wait()
        cancel_event.set()

    async with _unix_exec_host_server(sock_path, handle), asyncio.TaskGroup() as tg:
        tg.create_task(trigger_cancel())
        result, _payload = await run_in_subprocess(
            "import time; time.sleep(60)",
            exec_context(_AGENT_ID),
            cancel_event,
            30.0,
            accumulation_max_chars=1_000_000,
            parent=real_parent(),
            exec_dir=exec_dir,
        )

    assert isinstance(result, ExecCancelled)
    assert "working..." in result.output
    assert received_cancel == [{"id": 1, "method": "cancel"}]


async def test_run_in_subprocess_maps_exec_host_timeout_to_exec_timed_out(
    tmp_path: Path,
) -> None:
    exec_dir = tmp_path / "run" / "exec"
    sock_path = exec_host_socket(exec_dir)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await reader.readline()
            req = protocol.decode_object(raw.rstrip(b"\r\n"))
            writer.write(protocol.encode(protocol.output_chunk(req["id"], "before deadline\n")))
            await writer.drain()
            writer.write(
                protocol.encode(
                    protocol.ok(
                        req["id"],
                        {"returncode": 124, "cancelled": False, "timed_out": True},
                    )
                )
            )
            await writer.drain()
        finally:
            writer.close()

    async with _unix_exec_host_server(sock_path, handle):
        result, _payload = await run_in_subprocess(
            "import time; time.sleep(60)",
            exec_context(_AGENT_ID),
            asyncio.Event(),
            1.0,
            accumulation_max_chars=1_000_000,
            parent=real_parent(),
            exec_dir=exec_dir,
        )

    assert isinstance(result, ExecTimedOut)
    assert result.output == "before deadline\n"


async def test_run_in_subprocess_maps_exec_host_spawn_failure_to_exec_crashed(
    tmp_path: Path,
) -> None:
    exec_dir = tmp_path / "run" / "exec"
    sock_path = exec_host_socket(exec_dir)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await reader.readline()
            req = protocol.decode_object(raw.rstrip(b"\r\n"))
            writer.write(
                protocol.encode(
                    protocol.err(
                        req["id"],
                        protocol.ERROR,
                        "exec subprocess could not be spawned: [Errno 2] No such file or directory",
                    )
                )
            )
            await writer.drain()
        finally:
            writer.close()

    async with _unix_exec_host_server(sock_path, handle):
        result, payload = await run_in_subprocess(
            "print('never runs')",
            exec_context(_AGENT_ID),
            asyncio.Event(),
            10.0,
            accumulation_max_chars=1_000_000,
            parent=real_parent(),
            exec_dir=exec_dir,
        )

    assert isinstance(result, ExecCrashed)
    assert "exec subprocess could not be spawned" in result.output
    assert payload is None


async def test_run_in_subprocess_falls_back_to_local_child_when_exec_host_socket_absent(
    tmp_path: Path,
) -> None:
    exec_dir = tmp_path / "run" / "exec"
    assert not exec_host_socket(exec_dir).exists()

    result, payload = await run_in_subprocess(
        "print('local fallback')",
        exec_context(_AGENT_ID),
        asyncio.Event(),
        30.0,
        accumulation_max_chars=1_000_000,
        parent=real_parent(),
        exec_dir=exec_dir,
    )

    assert isinstance(result, ExecDone)
    assert result.output == "local fallback\n"
    assert payload is not None
    assert payload.kind == "done"
