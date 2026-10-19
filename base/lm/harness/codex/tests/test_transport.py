from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from base.lm.harness.codex.transport import (
    JsonObject,
    JsonRpcError,
    StdioAppServer,
    TransportClosedError,
)

SERVER = Path(__file__).with_name("fake_stdio_server.py")


def transport(tmp_path: Path, *, timeout: float = 5.0) -> StdioAppServer:
    return StdioAppServer(
        [sys.executable, str(SERVER)],
        cwd=tmp_path,
        request_timeout=timeout,
        stderr=asyncio.subprocess.DEVNULL,
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
    await server.start()
    pid = server.pid
    assert pid is not None
    await server.close()
    assert server.closed is True
    with pytest.raises(TransportClosedError):
        await server.request("echo", {})
