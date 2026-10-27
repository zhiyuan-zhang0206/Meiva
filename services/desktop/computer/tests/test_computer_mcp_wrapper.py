# meiva:note
# Unit tests for the computer MCP bridge's socket link. Covers Link (request/
# response framing + agent-id stamping) and ReconnectingSocketLink (automatic
# reconnect on transport errors). The MCP stdio surface itself is a thin
# pass-through (same shape as the browser wrapper), exercised against a live
# daemon in dev-cluster testing.

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest

from base.host.permissions_helper import client
from base.host.permissions_helper.client import PermissionsHelperError
from services.desktop.computer.mcp_wrapper import Link, ReconnectingSocketLink

from .slices import short_sock_dir


class FakeWriter:
    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.closed = False

    def write(self, b: bytes) -> None:
        self.written.append(b)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed


class FailingWriter(FakeWriter):
    def __init__(self, fail_stage: str) -> None:
        super().__init__()
        self.fail_stage = fail_stage

    def write(self, b: bytes) -> None:
        if self.fail_stage == "write":
            raise BrokenPipeError("socket broke")
        super().write(b)

    async def drain(self) -> None:
        if self.fail_stage == "drain":
            raise BrokenPipeError("socket broke")


class FakeReader:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""


class HangingReader(FakeReader):
    async def readline(self) -> bytes:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def _line(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj) + "\n").encode()


async def test_request_returns_result_on_ok() -> None:
    reader = FakeReader([_line({"id": 1, "ok": True, "result": {"x": 1}})])
    writer = FakeWriter()
    link = Link(reader, writer, timeout_reader=lambda: 1.0, agent_id=42)  # type: ignore[arg-type]
    assert await link.request({"method": "list_tools"}) == {"x": 1}
    sent = json.loads(writer.written[0])
    assert sent["agent_id"] == 42  # identity stamped on every request


async def test_request_without_identity() -> None:
    reader = FakeReader([_line({"id": 1, "ok": True, "result": None})])
    writer = FakeWriter()
    link = Link(reader, writer, timeout_reader=lambda: 1.0, agent_id=None)  # type: ignore[arg-type]
    await link.request({"method": "call_tool", "tool": "click", "args": {}})
    sent = json.loads(writer.written[0])
    assert sent["agent_id"] is None
    assert sent["tool"] == "click"


async def test_agent_identity_is_injected_for_each_request() -> None:
    reader = FakeReader(
        [
            _line({"id": 1, "ok": True, "result": None}),
            _line({"id": 2, "ok": True, "result": None}),
        ]
    )
    writer = FakeWriter()
    link = Link(reader, writer, timeout_reader=lambda: 1.0, agent_id=7)  # type: ignore[arg-type]
    await link.request({"method": "list_tools", "agent_id": 99})
    await link.request({"method": "call_tool", "tool": "click"})
    assert [json.loads(line)["agent_id"] for line in writer.written] == [7, 7]


async def test_request_raises_on_error_response() -> None:
    reader = FakeReader([_line({"id": 1, "ok": False, "error": "quota exceeded"})])
    link = Link(reader, FakeWriter(), timeout_reader=lambda: 1.0, agent_id=None)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="quota exceeded"):
        await link.request({"method": "call_tool", "tool": "x", "args": {}})


async def test_request_raises_on_closed_connection() -> None:
    link = Link(FakeReader([]), FakeWriter(), timeout_reader=lambda: 1.0, agent_id=None)  # type: ignore[arg-type]
    with pytest.raises(ConnectionError, match="closed"):
        await link.request({"method": "list_tools"})


async def test_request_raises_on_id_mismatch() -> None:
    reader = FakeReader([_line({"id": 99, "ok": True, "result": {}})])
    link = Link(reader, FakeWriter(), timeout_reader=lambda: 1.0, agent_id=None)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="response id"):
        await link.request({"method": "list_tools"})


@pytest.mark.parametrize("fail_stage", ["write", "drain"])
async def test_request_preserves_unknown_delivery_error(fail_stage: str) -> None:
    writer = FailingWriter(fail_stage)
    link = Link(FakeReader([]), writer, timeout_reader=lambda: 1.0, agent_id=None)  # type: ignore[arg-type]
    with pytest.raises(BrokenPipeError, match="socket broke"):
        await link.request({"method": "call_tool", "tool": "click", "args": {}})
    assert len(writer.written) == (1 if fail_stage == "drain" else 0)


# meiva:note
# The ReconnectingSocketLink cases below: reconnect on transport errors. Each replaces
# `_connect_once`, so `_SOCK` is never dialed.
_SOCK = "/nonexistent-wrapper-test.sock"


@pytest.mark.usefixtures("retry_waits")
async def test_reconnecting_link_redials_on_connection_error() -> None:
    attempts = 0
    writer = FakeWriter()

    async def _connect_once() -> Link:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionRefusedError("daemon not listening")
        return Link(
            cast(asyncio.StreamReader, FakeReader([_line({"id": 1, "ok": True, "result": "ok"})])),
            cast(asyncio.StreamWriter, writer),
            timeout_reader=lambda: 1.0,
            agent_id=None,
        )

    rl = ReconnectingSocketLink(sock=_SOCK, timeout_reader=lambda: 1.0, agent_id=None)
    rl._connect_once = _connect_once  # type: ignore[method-assign]
    assert await rl.request({"method": "ping"}) == "ok"
    assert attempts == 2
    assert len(writer.written) == 1


@pytest.mark.usefixtures("retry_waits")
async def test_reconnecting_link_never_retries_delivered_call() -> None:
    """A request whose response never arrives may have executed on the desktop —
    retrying could double-click. The error surfaces and the link reconnects."""

    attempts = 0
    writers: list[FakeWriter] = []

    async def _connect_once() -> Link:
        nonlocal attempts
        attempts += 1
        writer = FakeWriter()
        writers.append(writer)
        reader = (
            FakeReader([])
            if attempts == 1
            else FakeReader([_line({"id": 1, "ok": True, "result": "next"})])
        )
        return Link(reader, writer, timeout_reader=lambda: 1.0, agent_id=None)  # type: ignore[arg-type]

    rl = ReconnectingSocketLink(sock=_SOCK, timeout_reader=lambda: 1.0, agent_id=None)
    rl._connect_once = _connect_once  # type: ignore[method-assign]
    with pytest.raises(ConnectionError):
        await rl.request({"method": "call_tool", "tool": "click", "args": {}})
    assert attempts == 1
    assert [len(writer.written) for writer in writers] == [1]
    assert writers[0].closed
    assert await rl.request({"method": "list_tools"}) == "next"
    assert attempts == 2


@pytest.mark.usefixtures("retry_waits")
@pytest.mark.parametrize("fail_stage", ["write", "drain"])
async def test_reconnecting_link_does_not_retry_write_or_drain_failure(fail_stage: str) -> None:
    attempts = 0
    failed_writer = FailingWriter(fail_stage)
    success_writer = FakeWriter()

    async def _connect_once() -> Link:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return Link(FakeReader([]), failed_writer, timeout_reader=lambda: 1.0, agent_id=None)  # type: ignore[arg-type]
        return Link(
            cast(asyncio.StreamReader, FakeReader([_line({"id": 1, "ok": True, "result": "ok"})])),
            cast(asyncio.StreamWriter, success_writer),
            timeout_reader=lambda: 1.0,
            agent_id=None,
        )

    rl = ReconnectingSocketLink(sock=_SOCK, timeout_reader=lambda: 1.0, agent_id=None)
    rl._connect_once = _connect_once  # type: ignore[method-assign]
    with pytest.raises(BrokenPipeError, match="socket broke"):
        await rl.request({"method": "call_tool", "tool": "type", "args": {}})
    assert attempts == 1
    assert failed_writer.closed
    assert len(success_writer.written) == 0
    assert len(failed_writer.written) == (1 if fail_stage == "drain" else 0)
    assert await rl.request({"method": "list_tools"}) == "ok"
    assert attempts == 2


@pytest.mark.parametrize(
    "line,error",
    [
        (b"bad json\n", json.JSONDecodeError),
        (_line({"id": 99, "ok": True, "result": "wrong"}), RuntimeError),
    ],
)
async def test_reconnecting_link_does_not_retry_bad_response(
    line: bytes, error: type[Exception]
) -> None:
    attempts = 0
    writer = FakeWriter()

    async def _connect_once() -> Link:
        nonlocal attempts
        attempts += 1
        return Link(FakeReader([line]), writer, timeout_reader=lambda: 1.0, agent_id=None)  # type: ignore[arg-type]

    rl = ReconnectingSocketLink(sock=_SOCK, timeout_reader=lambda: 1.0, agent_id=None)
    rl._connect_once = _connect_once  # type: ignore[method-assign]
    with pytest.raises(error):
        await rl.request({"method": "call_tool", "tool": "click", "args": {}})
    assert attempts == 1
    assert len(writer.written) == 1
    assert writer.closed


async def test_reconnecting_link_times_out_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0
    writer = FakeWriter()

    async def _connect_once() -> Link:
        nonlocal attempts
        attempts += 1
        return Link(HangingReader([]), writer, timeout_reader=lambda: 0.01, agent_id=None)  # type: ignore[arg-type]

    rl = ReconnectingSocketLink(sock=_SOCK, timeout_reader=lambda: 0.01, agent_id=None)
    rl._connect_once = _connect_once  # type: ignore[method-assign]
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            rl.request({"method": "call_tool", "tool": "click", "args": {}}), timeout=0.2
        )
    assert attempts == 1
    assert len(writer.written) == 1
    assert writer.closed


# The computer MCP daemon uses this helper; pin its nested connection budgets here.


def test_connect_exhaustion_closes_every_socket_without_trailing_sleep(
    retry_waits: list[float],
) -> None:
    d, absent, cleanup = short_sock_dir()
    try:
        with pytest.raises(PermissionsHelperError) as error:
            client.connect(str(absent))
        assert f"permissions helper not reachable at {absent}:" in str(error.value)
        assert error.value.__context__ is None
        assert retry_waits == [0.2] * 4
    finally:
        cleanup(d)


def test_helper_connect_non_retryable_error_passes_through(
    retry_waits: list[float],
) -> None:
    d, _, cleanup = short_sock_dir()
    locked = d / "locked"
    locked.mkdir(mode=0o700)
    locked.chmod(0o000)
    try:
        with pytest.raises(PermissionError):
            client.connect(str(locked / "test.sock"))
    finally:
        locked.chmod(0o700)
        cleanup(d)
    assert retry_waits == []


def test_helper_unclassified_connect_failure_is_not_retried(
    retry_waits: list[float],
) -> None:
    d, _, cleanup = short_sock_dir()
    try:
        not_a_dir = d / "regular-file"
        not_a_dir.write_text("x")
        with pytest.raises(PermissionsHelperError, match="not reachable"):
            client.connect(str(not_a_dir / "test.sock"))
        assert retry_waits == []
    finally:
        cleanup(d)
