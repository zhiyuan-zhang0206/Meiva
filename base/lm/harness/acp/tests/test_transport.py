from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from base.lm.harness.acp.tests.support import collect, connect
from base.lm.harness.acp.transport import (
    AcpClosedError,
    AcpNotification,
    AcpRpcError,
)


async def test_initialize_reports_the_agent_identity() -> None:
    connection, _ = await connect()
    try:
        result = await connection.request("initialize", {"protocolVersion": 1})
        assert result["protocolVersion"] == 1
        assert result["agentInfo"]["name"] == "fake-acp-agent"
    finally:
        await connection.close()


async def test_notifications_stream_session_updates_in_order(tmp_path: Path) -> None:
    connection, _ = await connect()
    try:
        await connection.request("session/new", {"cwd": str(tmp_path), "mcpServers": []})
        await connection.request(
            "session/prompt", {"sessionId": "session-1", "prompt": [{"type": "text", "text": "hi"}]}
        )
        updates = await collect(connection, count=3)
        kinds = [
            item.params["update"]["sessionUpdate"]
            for item in updates
            if isinstance(item, AcpNotification)
        ]
        assert kinds == ["agent_thought_chunk", "agent_message_chunk", "usage_update"]
    finally:
        await connection.close()


async def test_permission_allow_is_answered_with_the_option_id() -> None:
    connection, seen = await connect(permission_allows=True)
    try:
        await connection.request(
            "session/prompt",
            {"sessionId": "session-1", "prompt": [{"type": "text", "text": "permission"}]},
        )
        updates = await collect(connection, count=1)
        texts = [
            item.params["update"]["content"]["text"]
            for item in updates
            if isinstance(item, AcpNotification)
            and item.params["update"]["sessionUpdate"] == "agent_message_chunk"
        ]
        assert texts == ["decision:allow-once"]
        assert seen[0].session_id == "session-1"
        assert seen[0].tool_call["title"] == "bash"
    finally:
        await connection.close()


async def test_missing_permission_handler_reads_as_cancelled() -> None:
    connection, _ = await connect(permission_allows=None)
    try:
        await connection.request(
            "session/prompt",
            {"sessionId": "session-1", "prompt": [{"type": "text", "text": "permission"}]},
        )
        updates = await collect(connection, count=1)
        texts = [
            item.params["update"]["content"]["text"]
            for item in updates
            if isinstance(item, AcpNotification)
            and item.params["update"]["sessionUpdate"] == "agent_message_chunk"
        ]
        assert texts == ["decision:cancelled"]
    finally:
        await connection.close()


async def test_cancel_notification_settles_the_in_flight_prompt() -> None:
    connection, _ = await connect()
    try:
        prompt = asyncio.create_task(
            connection.request(
                "session/prompt",
                {"sessionId": "session-1", "prompt": [{"type": "text", "text": "slow"}]},
            )
        )
        await asyncio.sleep(0.05)
        assert not prompt.done()
        await connection.notify("session/cancel", {"sessionId": "session-1"})
        assert await asyncio.wait_for(prompt, timeout=2.0) == {"stopReason": "cancelled"}
    finally:
        await connection.close()


async def test_unknown_method_is_a_jsonrpc_error_with_its_code() -> None:
    connection, _ = await connect()
    try:
        with pytest.raises(AcpRpcError) as caught:
            await connection.request("session/does-not-exist", {})
        assert caught.value.code == -32601
        assert "session/does-not-exist" in caught.value.message
    finally:
        await connection.close()


async def test_non_protocol_stdout_is_recorded_not_fatal(tmp_path: Path) -> None:
    connection, _ = await connect(extra=("--garbage",))
    try:
        await asyncio.sleep(0.05)
        assert any("non-JSON stdout" in line for line in connection.stderr_tail)
        result = await connection.request("session/new", {"cwd": str(tmp_path), "mcpServers": []})
        assert result["sessionId"] == "session-1"
    finally:
        await connection.close()


async def test_close_ends_the_child_process() -> None:
    connection, _ = await connect()
    pid = connection.pid
    assert pid is not None
    await connection.close()
    await asyncio.sleep(0.05)
    with pytest.raises(OSError):
        os.kill(pid, 0)


async def test_request_after_close_is_refused(tmp_path: Path) -> None:
    connection, _ = await connect()
    await connection.close()
    with pytest.raises(AcpClosedError):
        await connection.request("session/new", {"cwd": str(tmp_path), "mcpServers": []})
