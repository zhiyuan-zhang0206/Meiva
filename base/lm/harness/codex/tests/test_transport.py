from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from base.lm.harness.codex.frames import JsonObject, JsonRpcError, TransportClosedError, as_object
from base.lm.harness.codex.transport import StdioAppServer

SERVER = Path(__file__).with_name("fake_stdio_server.py")


def transport(
    tmp_path: Path, *, timeout: float = 5.0, max_frame_bytes: int | None = None
) -> StdioAppServer:
    kwargs: dict[str, int] = (
        {"max_frame_bytes": max_frame_bytes} if max_frame_bytes is not None else {}
    )
    return StdioAppServer(
        [sys.executable, str(SERVER)],
        cwd=tmp_path,
        environ={},
        request_timeout=timeout,
        stderr=asyncio.subprocess.DEVNULL,
        **kwargs,
    )


async def test_request_reply_is_correlated_by_id(tmp_path: Path) -> None:
    server = transport(tmp_path)
    await server.start()
    try:
        assert await server.request("initialize", {}) == {"userAgent": "fake-stdio"}
        assert await server.request("echo", {"x": 1}) == {"echo": "echo"}
    finally:
        await server.close()


async def test_notification_reaches_the_handler(tmp_path: Path) -> None:
    seen: list[tuple[str, JsonObject]] = []
    server = transport(tmp_path)
    server.on_notification(lambda method, params: seen.append((method, params)))
    await server.start()
    try:
        await server.request("emit", {})
        for _ in range(100):
            if seen:
                break
            await asyncio.sleep(0.01)
        assert seen == [("test/notification", {"value": 1})]
    finally:
        await server.close()


async def test_server_request_is_answered_on_the_same_pipe(tmp_path: Path) -> None:
    async def handler(method: str, params: JsonObject) -> JsonObject:
        assert method == "item/commandExecution/requestApproval"
        assert params == {"command": "echo hi"}
        return {"decision": "decline"}

    server = transport(tmp_path)
    server.on_request(handler)
    await server.start()
    try:
        result = await server.request("ask", {})
        assert result == {"answer": {"decision": "decline"}}
    finally:
        await server.close()


async def test_json_rpc_error_carries_the_code(tmp_path: Path) -> None:
    server = transport(tmp_path)
    await server.start()
    try:
        with pytest.raises(JsonRpcError) as info:
            await server.request("boom", {})
        assert info.value.code == -32000
        assert "boom" in info.value.message
    finally:
        await server.close()


async def test_request_times_out_and_fails_fast_after_the_peer_exits(tmp_path: Path) -> None:
    server = transport(tmp_path, timeout=0.2)
    await server.start()
    try:
        with pytest.raises(TimeoutError):
            await server.request("hang", {})
        await server.request("quit", {})
        for _ in range(100):
            if server.closed:
                break
            await asyncio.sleep(0.01)
        assert server.closed is True
        with pytest.raises(TransportClosedError):
            await server.request("echo", {})
    finally:
        await server.close()


async def test_close_terminates_the_process(tmp_path: Path) -> None:
    server = transport(tmp_path)
    assert server.returncode is None
    await server.start()
    pid = server.pid
    assert pid is not None
    await server.close()
    assert server.closed is True
    assert server.returncode is not None
    with pytest.raises(TransportClosedError):
        await server.request("echo", {})


@pytest.mark.parametrize("environment", [{}, {"SESSION_VALUE": "captured"}])
async def test_transport_copies_only_its_explicit_environment_before_start(
    tmp_path: Path, environment: dict[str, str]
) -> None:
    supplied = dict(environment)
    server = StdioAppServer(
        [sys.executable, str(SERVER)], cwd=tmp_path, environ=supplied, request_timeout=5
    )
    supplied["SESSION_VALUE"] = "changed-after-construction"
    try:
        await server.start()
        observed = await server.request("environment", {})
        assert observed.get("SESSION_VALUE") == environment.get("SESSION_VALUE")
        assert "PYTEST_CURRENT_TEST" not in observed
        assert "MEIVA_HOME" not in observed
    finally:
        await server.close()


async def test_reply_above_eight_mib_is_received_whole(tmp_path: Path) -> None:
    payload_bytes = 10 * 1024 * 1024
    server = transport(tmp_path, timeout=15.0)
    await server.start()
    try:
        result = await server.request("large_reply", {"bytes": payload_bytes})
        thread = as_object(result.get("thread"))
        assert thread["id"] == "thread-big"
        blob = thread["blob"]
        assert isinstance(blob, str) and len(blob) == payload_bytes
        assert await server.request("echo", {}) == {"echo": "echo"}
    finally:
        await server.close()


async def test_trailing_frame_without_newline_at_eof_settles_request(tmp_path: Path) -> None:
    payload_bytes = 128 * 1024
    server = transport(tmp_path)
    await server.start()
    try:
        result = await server.request("eof_reply", {"bytes": payload_bytes})
        assert result["eof"] is True
        blob = result["blob"]
        assert isinstance(blob, str) and len(blob) == payload_bytes
    finally:
        await server.close()


@pytest.mark.parametrize("method", ["large_reply", "unterminated_flood"])
async def test_frame_exceeding_max_frame_bytes_closes_the_transport(
    tmp_path: Path, method: str
) -> None:
    server = transport(tmp_path, timeout=5.0, max_frame_bytes=4096)
    await server.start()
    try:
        with pytest.raises(TransportClosedError, match="stdout frame exceeded 4096 bytes"):
            _ = await server.request(method, {"bytes": 16384})
        assert server.closed is True
    finally:
        await server.close()
