from __future__ import annotations

import asyncio
import os
import random
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from base.api_contracts.agent_contract import Harness
from base.clock.tests.fakes import clock_over, system_clock
from base.clock.tests.manual_timers import ManualTimers
from base.lm.harness import Handoff, HarnessBridge, HarnessClosedError, HarnessTurnFaultedError
from base.lm.harness.base_session import SessionBinding
from base.lm.harness.claude import BridgeConfig
from base.lm.harness.claude.session import ClaudeSession
from base.lm.harness.claude.tests.fake_sdk import (
    Block,
    Emit,
    FakeSDK,
    ResultMessage,
    Say,
    ScriptedProbe,
    TaskNotificationMessage,
    TaskStartedMessage,
)
from base.lm.harness.codex import CodexConfig
from base.lm.harness.codex.frames import JsonObject, RequestParams
from base.lm.harness.codex.session import CodexSession
from base.lm.harness.codex.tests.fake_app_server import FakeAppServer
from base.lm.harness.events import UnsolicitedOutput
from base.lm.harness.registration import RegistrationStore, SecureRandom
from base.lm.harness.registry import FileSessionRegistry


async def until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


def handoff(tmp_path: Path) -> Handoff:
    return Handoff(agent_id=7, instruction="Acknowledge only.", machine="test", workspace=tmp_path)


async def test_claude_cancel_during_first_turn_co_delivers_handoff_and_keeps_session(
    tmp_path: Path,
) -> None:
    sdk = FakeSDK([[Block()], [Say("next reply")]])
    session = ClaudeSession(
        BridgeConfig(cwd=tmp_path),
        environ={},
        sdk=sdk,
        probe=ScriptedProbe(),
        binding=SessionBinding(),
        clock=system_clock(),
    )
    bridge = HarnessBridge(
        Harness.CLAUDE_CODE,
        7,
        lambda _: session,
        handoff=handoff(tmp_path),
        registration_ttl_seconds=3600,
        clock=system_clock(),
        handle_rng=SecureRandom(random.SystemRandom()),
        controller_pid=os.getpid(),
    )
    task = asyncio.create_task(bridge.run_turn("user work", key="inbound-1"))
    try:
        await until(lambda: bool(sdk.clients and sdk.clients[0].queries))
        await bridge.interrupt()
        assert (await asyncio.wait_for(task, timeout=2)).interrupted
        assert sdk.clients[0].queries == [f"{handoff(tmp_path).render().strip()}\n\nuser work"]
        assert not sdk.clients[0].disconnected
        assert session.pending_turns == ()
        assert bridge.session_id == session.session_id
        assert (await bridge.run_turn("next", key="inbound-2")).text == "next reply"
        assert len(sdk.clients) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await bridge.close()


async def test_claude_background_report_survives_a_later_foreground_cancel(
    tmp_path: Path,
) -> None:
    delivered: list[UnsolicitedOutput] = []
    started = TaskStartedMessage(subtype="task_started", data={}, task_id="independent")
    sdk = FakeSDK([[Emit(started), Say("A started a task")], [Block()], [Say("C ran")]])
    session = ClaudeSession(
        BridgeConfig(cwd=tmp_path),
        environ={},
        sdk=sdk,
        probe=ScriptedProbe(),
        binding=SessionBinding(unsolicited=delivered.append),
        clock=system_clock(),
    )
    bridge = HarnessBridge(
        Harness.CLAUDE_CODE,
        7,
        lambda _: session,
        registration_ttl_seconds=3600,
        clock=system_clock(),
        handle_rng=SecureRandom(random.SystemRandom()),
        controller_pid=os.getpid(),
    )
    try:
        assert (await bridge.run_turn("A", key="a")).text == "A started a task"
        client = sdk.clients[0]
        ack_gate = asyncio.Event()
        client.interrupt_ack_gate = ack_gate
        async with asyncio.TaskGroup() as group:
            foreground = group.create_task(bridge.run_turn("B", key="b"))
            await until(lambda: client.queries == ["A", "B"])
            cancellation = group.create_task(bridge.interrupt())
            assert (await asyncio.wait_for(foreground, timeout=2)).interrupted
            assert not client.interrupt_acknowledged.is_set()
            _ = cancellation.cancel()

        ack_gate.set()
        await asyncio.wait_for(client.interrupt_acknowledged.wait(), timeout=2)
        assert not client.disconnected
        assert client.stopped == []
        client.push(
            TaskNotificationMessage(
                subtype="task_notification",
                data={},
                task_id="independent",
                status="completed",
                output_file="/var/tasks/independent.output",
                summary="Independent task finished.",
            )
        )
        await until(lambda: bool(delivered))
        client.push(ResultMessage(origin={"kind": "task-notification"}))
        assert (await asyncio.wait_for(bridge.run_turn("C", key="c"), timeout=2)).text == "C ran"
        [report] = delivered
        assert report.source == "task-notification"
        assert "independent" in report.text
        assert client.queries == ["A", "B", "C"]
        assert len(sdk.clients) == 1
    finally:
        await bridge.close()


@pytest.mark.parametrize("disconnect", [False, True])
async def test_codex_cancel_waits_for_matching_terminal_or_session_failure(
    tmp_path: Path, disconnect: bool
) -> None:
    server = FakeAppServer(pid=None)
    session = CodexSession(
        CodexConfig(cwd=tmp_path),
        environ={},
        transport=server,
        binding=SessionBinding(),
        clock=system_clock(),
    )
    bridge = HarnessBridge(
        Harness.CODEX,
        7,
        lambda _: session,
        handoff=handoff(tmp_path),
        registration_ttl_seconds=3600,
        clock=system_clock(),
        handle_rng=SecureRandom(random.SystemRandom()),
        controller_pid=os.getpid(),
    )
    task = asyncio.create_task(bridge.run_turn("user work", key="inbound-1"))
    try:
        await until(lambda: len(server.turn_ids) == 1)
        await bridge.interrupt()
        assert not task.done()
        server.emit(
            "turn/completed",
            {"threadId": server.thread_id, "turn": {"id": "other-work", "status": "interrupted"}},
        )
        assert not task.done()
        if disconnect:
            await server.close()
            with pytest.raises(HarnessClosedError, match="session failed"):
                await asyncio.wait_for(task, timeout=2)
            assert bridge.session_id is None
        else:
            server.complete(status="interrupted")
            assert (await asyncio.wait_for(task, timeout=2)).interrupted
            assert not server.closed
            assert bridge.session_id == session.session_id
        assert len(server.params_for("turn/start")) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await bridge.close()


async def test_a_quiet_turn_keeps_its_attachment_after_the_original_expiry(tmp_path: Path) -> None:
    store = RegistrationStore(tmp_path / "registrations")
    timers = ManualTimers()
    clock = clock_over(timers)
    server = FakeAppServer(pid=None)
    session = CodexSession(
        CodexConfig(cwd=tmp_path),
        environ={},
        transport=server,
        binding=SessionBinding(),
        clock=clock,
    )
    bridge = HarnessBridge(
        Harness.CODEX,
        7,
        lambda _: session,
        registrations=store,
        registration_ttl_seconds=3600,
        clock=clock,
        handle_rng=SecureRandom(random.SystemRandom()),
        controller_pid=os.getpid(),
    )

    turn = asyncio.create_task(bridge.run_turn("wait quietly", key="inbound-1"))
    try:
        await until(lambda: bool(server.turn_ids))
        first = store.read(7)
        assert first is not None
        expiry = datetime.fromisoformat(first.expires_at)
        await until(lambda: timers.now() > expiry)

        assert not turn.done()
        assert store.sweep(now=timers.now()) == []
        renewed = store.read(7)
        assert renewed is not None
        assert renewed.created_at == first.created_at
        assert renewed.handle_sha256 == first.handle_sha256
        assert renewed.external_session_id == first.external_session_id == session.session_id
        server.complete()
        assert not (await asyncio.wait_for(turn, timeout=2)).is_error
    finally:
        _ = turn.cancel()
        _ = await asyncio.gather(turn, return_exceptions=True)
        await bridge.close()
    assert store.read(7) is None


async def test_confirmed_failed_first_turn_leaves_the_session_reusable(tmp_path: Path) -> None:
    sdk = FakeSDK(
        [
            [Emit(ResultMessage(is_error=True, subtype="error_during_execution"))],
            [Say("next turn")],
        ]
    )
    session = ClaudeSession(
        BridgeConfig(cwd=tmp_path),
        environ={},
        sdk=sdk,
        probe=ScriptedProbe(),
        binding=SessionBinding(),
        clock=system_clock(),
    )
    bridge = HarnessBridge(
        Harness.CLAUDE_CODE,
        7,
        lambda _: session,
        handoff=handoff(tmp_path),
        registration_ttl_seconds=3600,
        clock=system_clock(),
        handle_rng=SecureRandom(random.SystemRandom()),
        controller_pid=os.getpid(),
    )
    try:
        with pytest.raises(HarnessTurnFaultedError, match="failed"):
            await asyncio.wait_for(bridge.run_turn("user work", key="inbound-1"), timeout=2)
        assert sdk.clients[0].queries == [f"{handoff(tmp_path).render().strip()}\n\nuser work"]
        assert not sdk.clients[0].disconnected
        followup = await bridge.run_turn("follow up", key="inbound-2")
        assert followup.text == "next turn"
        assert len(sdk.clients) == 1
        assert sdk.clients[0].queries[-1] == "follow up"
    finally:
        await bridge.close()


async def test_completed_key_replays_without_waiting_for_another_event(tmp_path: Path) -> None:
    sdk = FakeSDK([[Say("first reply")], [Say("next reply")]])
    session = ClaudeSession(
        BridgeConfig(cwd=tmp_path),
        environ={},
        sdk=sdk,
        probe=ScriptedProbe(),
        binding=SessionBinding(),
        clock=system_clock(),
    )
    bridge = HarnessBridge(
        Harness.CLAUDE_CODE,
        7,
        lambda _: session,
        registration_ttl_seconds=3600,
        clock=system_clock(),
        handle_rng=SecureRandom(random.SystemRandom()),
        controller_pid=os.getpid(),
    )
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
        self.allow_start = asyncio.Event()

    async def request(
        self, method: str, params: RequestParams, *, timeout: float | None = None
    ) -> JsonObject:
        if method == "turn/start":
            self.starting.set()
            await self.allow_start.wait()
        result = await super().request(method, params, timeout=timeout)
        if method == "turn/interrupt":
            self.complete(status="interrupted")
        return result


async def test_cancel_waits_for_codex_provider_id(
    tmp_path: Path,
) -> None:
    server = StartingServer()
    session = CodexSession(
        CodexConfig(cwd=tmp_path),
        environ={},
        transport=server,
        binding=SessionBinding(),
        clock=system_clock(),
    )
    bridge = HarnessBridge(
        Harness.CODEX,
        7,
        lambda _: session,
        registration_ttl_seconds=3600,
        clock=system_clock(),
        handle_rng=SecureRandom(random.SystemRandom()),
        controller_pid=os.getpid(),
    )
    task = asyncio.create_task(bridge.run_turn("user work", key="inbound-1"))
    try:
        await asyncio.wait_for(server.starting.wait(), timeout=2)
        cancel = asyncio.create_task(bridge.interrupt())
        await asyncio.sleep(0)
        assert not cancel.done()
        assert not server.params_for("turn/interrupt")
        server.allow_start.set()
        await asyncio.wait_for(cancel, timeout=2)
        assert server.params_for("turn/interrupt") == [
            {"threadId": server.thread_id, "turnId": server.turn_ids[0]}
        ]
        assert (await asyncio.wait_for(task, timeout=2)).interrupted
        assert not server.closed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await bridge.close()


async def test_interrupt_error_leaves_foreground_running(tmp_path: Path) -> None:
    server = FakeAppServer(pid=None)
    session = CodexSession(
        CodexConfig(cwd=tmp_path),
        environ={},
        transport=server,
        binding=SessionBinding(),
        clock=system_clock(),
    )
    bridge = HarnessBridge(
        Harness.CODEX,
        7,
        lambda _: session,
        registration_ttl_seconds=3600,
        clock=system_clock(),
        handle_rng=SecureRandom(random.SystemRandom()),
        controller_pid=os.getpid(),
    )
    task = asyncio.create_task(bridge.run_turn("user work", key="inbound-1"))
    try:
        await until(lambda: bool(server.turn_ids))

        def fail() -> None:
            raise RuntimeError("interrupt connection failed")

        server.on_interrupt = fail
        with pytest.raises(RuntimeError, match="interrupt connection failed"):
            await bridge.interrupt()
        assert not task.done()
        assert not server.closed
        server.complete()
        assert not (await asyncio.wait_for(task, timeout=2)).interrupted
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await bridge.close()


async def test_next_turn_never_reuses_previous_turns_completion_after_cancel_request(
    tmp_path: Path,
) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    servers: list[FakeAppServer] = []

    def factory(_handle: str) -> CodexSession:
        server = FakeAppServer(pid=None)
        servers.append(server)
        return CodexSession(
            CodexConfig(cwd=tmp_path),
            environ={},
            transport=server,
            binding=SessionBinding(7, registry),
            clock=system_clock(),
        )

    bridge = HarnessBridge(
        Harness.CODEX,
        7,
        factory,
        registration_ttl_seconds=3600,
        clock=system_clock(),
        handle_rng=SecureRandom(random.SystemRandom()),
        controller_pid=os.getpid(),
    )
    task = asyncio.create_task(bridge.run_turn("old work", key="inbound-1"))
    try:
        await until(lambda: bool(servers and servers[0].turn_ids))
        await bridge.interrupt()
        servers[0].message("old completion")
        servers[0].complete()
        assert not (await asyncio.wait_for(task, timeout=2)).interrupted
        task = asyncio.create_task(bridge.run_turn("new work", key="inbound-2"))
        await until(lambda: len(servers[0].turn_ids) == 2)
        servers[0].message("new completion")
        servers[0].complete()
        turn = await asyncio.wait_for(task, timeout=2)
        assert turn.text == "new completion"
        assert turn.prelude == ()
        assert len(servers) == 1
        replay = await asyncio.wait_for(bridge.run_turn("new work", key="inbound-2"), timeout=2)
        assert replay.replayed
        assert replace(replay, replayed=False) == turn
        assert len(servers[0].turn_ids) == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await bridge.close()


async def test_observed_process_residue_blocks_subsequent_sessions(tmp_path: Path) -> None:
    sdk = FakeSDK([[Say("done")]])
    probe = ScriptedProbe()
    session = ClaudeSession(
        BridgeConfig(cwd=tmp_path, cancel_settle_seconds=0),
        environ={},
        sdk=sdk,
        probe=probe,
        binding=SessionBinding(),
        clock=system_clock(),
    )
    bridge = HarnessBridge(
        Harness.CLAUDE_CODE,
        7,
        lambda _: session,
        registration_ttl_seconds=3600,
        clock=system_clock(),
        handle_rng=SecureRandom(random.SystemRandom()),
        controller_pid=os.getpid(),
    )
    await bridge.run_turn("work", key="inbound-1")
    probe.spawn(5150, "sleep 40")
    with pytest.raises(RuntimeError, match=r"process.*running"):
        await bridge.close()
    with pytest.raises(HarnessClosedError, match="previous harness session could not close"):
        await bridge.run_turn("next", key="inbound-2")
    with pytest.raises(HarnessClosedError, match="previous harness session could not close"):
        await bridge.close()
    assert len(sdk.clients) == 1
