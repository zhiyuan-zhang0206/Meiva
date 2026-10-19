from __future__ import annotations

import asyncio
from pathlib import Path

from base.lm.harness.codex.tests.fake_app_server import CODEX_PID
from base.lm.harness.codex.tests.support import start
from base.lm.harness.events import SessionFaulted, TurnCompleted

TOOL_MARKER = "sleep 40"


async def test_interrupt_asserts_the_tool_children_are_reaped(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        ticket = harness.session.submit("run the long command")
        await harness.turn_started()
        harness.server.command_started(command=TOOL_MARKER)
        harness.probe.spawn(5150, TOOL_MARKER)
        harness.server.on_interrupt = lambda: harness.probe.reap(5150)
        report = await harness.session.interrupt()
        harness.server.complete(status="interrupted")
        assert report.turn_id == ticket.turn_id
        assert report.settled is True
        assert report.remaining == ()
        assert [observation.pid for observation in report.observed] == [5150]
        assert report.cli_pid == CODEX_PID
        assert report.cli_alive is True
        assert "turn/interrupt" in harness.server.methods()
        completed = await harness.wait_for(lambda event: isinstance(event, TurnCompleted))
        assert completed.interrupted is True
        assert completed.is_error is True
    finally:
        await harness.stop()


async def test_surviving_child_is_reported_as_residue(tmp_path: Path) -> None:
    harness = await start(tmp_path, cancel_settle_seconds=0.1)
    try:
        harness.session.submit("run the long command")
        await harness.turn_started()
        harness.probe.spawn(5150, TOOL_MARKER)
        report = await harness.session.interrupt()
        assert report.settled is False
        assert [observation.pid for observation in report.remaining] == [5150]
        assert report.remaining[0].command == TOOL_MARKER
        assert report.waited_ms >= 100.0
    finally:
        await harness.stop()


async def test_unobservable_app_server_is_not_a_pass(tmp_path: Path) -> None:
    harness = await start(tmp_path, observe_pid=False)
    try:
        harness.session.submit("run something")
        await harness.turn_started()
        report = await harness.session.interrupt()
        assert report.settled is None
        assert report.cli_pid is None
        assert report.cli_alive is None
    finally:
        await harness.stop()


async def test_turn_timeout_interrupts_the_turn(tmp_path: Path) -> None:
    harness = await start(tmp_path, turn_timeout_seconds=0.05)
    try:
        ticket = harness.session.submit("hang forever")
        await harness.turn_started()
        fault = await harness.wait_for(
            lambda event: isinstance(event, SessionFaulted) and "exceeded" in event.error
        )
        assert fault.turn_id == ticket.turn_id
        assert "turn/interrupt" in harness.server.methods()
        harness.server.complete(status="interrupted")
        completed = await harness.wait_for(lambda event: isinstance(event, TurnCompleted))
        assert completed.interrupted is True
    finally:
        await harness.stop()


async def test_session_stays_usable_after_interrupt(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        harness.session.submit("run something")
        await harness.turn_started()
        await harness.session.interrupt()
        harness.server.complete(status="interrupted")
        await harness.wait_for(lambda event: isinstance(event, TurnCompleted))
        second = harness.session.submit("next one")
        await harness.turn_started(2)
        harness.server.message("still alive")
        harness.server.complete()
        assert (await asyncio.wait_for(second.outcome(), 2)).text == "still alive"
    finally:
        await harness.stop()
