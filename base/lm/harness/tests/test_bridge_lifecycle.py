from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from base.api_contracts.agent_contract import Harness
from base.lm.harness import Handoff, HarnessBridge, HarnessClosedError, HarnessTurnFaultedError
from base.lm.harness.claude import BridgeConfig, ClaudeSession
from base.lm.harness.claude.tests.fake_sdk import (
    Block,
    Emit,
    FakeSDK,
    ResultMessage,
    Say,
    ScriptedProbe,
)
from base.lm.harness.codex import CodexConfig, CodexSession
from base.lm.harness.codex.tests.fake_app_server import FakeAppServer
from base.lm.harness.codex.transport import JsonObject, RequestParams
from base.lm.harness.registry import FileSessionRegistry


async def until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


def handoff(tmp_path: Path) -> Handoff:
    return Handoff(agent_id=7, instruction="Acknowledge only.", machine="test", workspace=tmp_path)


async def test_claude_cancel_during_handoff_does_not_start_the_user_turn(tmp_path: Path) -> None:
    sdk = FakeSDK([[Block()], [Say("should not run")]])
    session = ClaudeSession(BridgeConfig(cwd=tmp_path), environ={}, sdk=sdk, probe=ScriptedProbe())
    bridge = HarnessBridge(
        Harness.CLAUDE_CODE,
        7,
        lambda _: session,
        handoff=handoff(tmp_path),
        registration_ttl_seconds=3600,
    )
    task = asyncio.create_task(bridge.run_turn("user work", key="inbound-1"))
    try:
        await until(lambda: bool(sdk.clients and sdk.clients[0].queries))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await bridge.interrupt()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert sdk.clients[0].queries == [handoff(tmp_path).render().strip()]
        assert sdk.clients[0].disconnected
        assert session.pending_turns == ()
        assert bridge.session_id is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await bridge.close()


async def test_codex_cancel_during_handoff_does_not_start_the_user_turn(tmp_path: Path) -> None:
    server = FakeAppServer(pid=None)
    session = CodexSession(CodexConfig(cwd=tmp_path), environ={}, transport=server)
    bridge = HarnessBridge(
        Harness.CODEX,
        7,
        lambda _: session,
        handoff=handoff(tmp_path),
        registration_ttl_seconds=3600,
    )
    task = asyncio.create_task(bridge.run_turn("user work", key="inbound-1"))
    try:
        await until(lambda: len(server.turn_ids) == 1)
        server.on_interrupt = lambda: server.complete(status="interrupted")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await bridge.interrupt()
        await asyncio.sleep(0)
        assert len(server.params_for("turn/start")) == 1
        assert server.closed
        assert session.pending_turns == ()
        assert bridge.session_id is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await bridge.close()


async def test_failed_handoff_never_submits_user_work(tmp_path: Path) -> None:
    sdk = FakeSDK(
        [
            [Emit(ResultMessage(is_error=True, subtype="error_during_execution"))],
            [Say("should not run")],
        ]
    )
    session = ClaudeSession(BridgeConfig(cwd=tmp_path), environ={}, sdk=sdk, probe=ScriptedProbe())
    bridge = HarnessBridge(
        Harness.CLAUDE_CODE,
        7,
        lambda _: session,
        handoff=handoff(tmp_path),
        registration_ttl_seconds=3600,
    )
    try:
        with pytest.raises(HarnessTurnFaultedError, match="handoff"):
            await asyncio.wait_for(bridge.run_turn("user work", key="inbound-1"), timeout=2)
        assert sdk.clients[0].queries == [handoff(tmp_path).render().strip()]
        assert sdk.clients[0].disconnected
    finally:
        await bridge.close()


async def test_completed_key_replays_without_waiting_for_another_event(tmp_path: Path) -> None:
    sdk = FakeSDK([[Say("first reply")], [Say("next reply")]])
    session = ClaudeSession(BridgeConfig(cwd=tmp_path), environ={}, sdk=sdk, probe=ScriptedProbe())
    bridge = HarnessBridge(Harness.CLAUDE_CODE, 7, lambda _: session, registration_ttl_seconds=3600)
    try:
        original = await bridge.run_turn("first", key="inbound-1")
        replay = await asyncio.wait_for(bridge.run_turn("first", key="inbound-1"), timeout=2)
        following = await bridge.run_turn("next", key="inbound-2")
        assert replay.replayed
        assert replace(replay, replayed=False) == original
        assert following.text == "next reply"
        assert following.prelude == ()
        assert sdk.clients[0].queries == ["first", "next"]
    finally:
        await bridge.close()


class StartingServer(FakeAppServer):
    def __init__(self) -> None:
        super().__init__(pid=None)
        self.starting = asyncio.Event()
        self.start_cancelled = False

    async def request(
        self, method: str, params: RequestParams, *, timeout: float | None = None
    ) -> JsonObject:
        if method == "turn/start":
            self.starting.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.start_cancelled = True
                raise
        return await super().request(method, params, timeout=timeout)


async def test_cancel_while_codex_turn_start_has_no_provider_id_closes_transport(
    tmp_path: Path,
) -> None:
    server = StartingServer()
    session = CodexSession(CodexConfig(cwd=tmp_path), environ={}, transport=server)
    bridge = HarnessBridge(Harness.CODEX, 7, lambda _: session, registration_ttl_seconds=3600)
    task = asyncio.create_task(bridge.run_turn("user work", key="inbound-1"))
    try:
        await asyncio.wait_for(server.starting.wait(), timeout=2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await bridge.interrupt()
        assert server.start_cancelled
        assert server.closed
        assert server.turn_ids == []
        assert bridge.session_id is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await bridge.close()


async def test_interrupt_error_still_retires_session(tmp_path: Path) -> None:
    server = FakeAppServer(pid=None)
    session = CodexSession(CodexConfig(cwd=tmp_path), environ={}, transport=server)
    bridge = HarnessBridge(Harness.CODEX, 7, lambda _: session, registration_ttl_seconds=3600)
    task = asyncio.create_task(bridge.run_turn("user work", key="inbound-1"))
    try:
        await until(lambda: bool(server.turn_ids))

        def fail() -> None:
            raise RuntimeError("interrupt connection failed")

        server.on_interrupt = fail
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        with pytest.raises(RuntimeError, match="interrupt connection failed"):
            await bridge.interrupt()
        assert server.closed
        assert bridge.session_id is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await bridge.close()


async def test_resumed_turn_never_reuses_the_cancelled_sessions_completion(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    servers: list[FakeAppServer] = []

    def factory(_handle: str) -> CodexSession:
        server = FakeAppServer(pid=None)
        servers.append(server)
        return CodexSession(
            CodexConfig(cwd=tmp_path), environ={}, transport=server, agent_id=7, registry=registry
        )

    bridge = HarnessBridge(Harness.CODEX, 7, factory, registration_ttl_seconds=3600)
    task = asyncio.create_task(bridge.run_turn("old work", key="inbound-1"))
    try:
        await until(lambda: bool(servers and servers[0].turn_ids))
        servers[0].message("old completion")
        servers[0].complete()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await bridge.interrupt()
        assert servers[0].closed
        task = asyncio.create_task(bridge.run_turn("new work", key="inbound-2"))
        await until(lambda: len(servers) == 2 and bool(servers[1].turn_ids))
        servers[1].message("new completion")
        servers[1].complete()
        turn = await asyncio.wait_for(task, timeout=2)
        assert turn.text == "new completion"
        assert turn.prelude == ()
        assert "thread/resume" in servers[1].methods()
        replay = await asyncio.wait_for(bridge.run_turn("new work", key="inbound-2"), timeout=2)
        assert replay.replayed
        assert replace(replay, replayed=False) == turn
        assert len(servers[1].turn_ids) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await bridge.close()


async def test_observed_process_residue_blocks_subsequent_sessions(tmp_path: Path) -> None:
    sdk = FakeSDK([[Say("done")]])
    probe = ScriptedProbe()
    session = ClaudeSession(
        BridgeConfig(cwd=tmp_path, cancel_settle_seconds=0), environ={}, sdk=sdk, probe=probe
    )
    bridge = HarnessBridge(Harness.CLAUDE_CODE, 7, lambda _: session, registration_ttl_seconds=3600)
    await bridge.run_turn("work", key="inbound-1")
    probe.spawn(5150, "sleep 40")
    with pytest.raises(RuntimeError, match=r"process.*running"):
        await bridge.close()
    with pytest.raises(HarnessClosedError, match="previous harness session could not close"):
        await bridge.run_turn("next", key="inbound-2")
    with pytest.raises(HarnessClosedError, match="previous harness session could not close"):
        await bridge.close()
    assert len(sdk.clients) == 1
