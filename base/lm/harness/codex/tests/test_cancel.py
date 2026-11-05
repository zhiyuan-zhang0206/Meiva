from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from base.clock.tests.fakes import clock_over
from base.clock.tests.manual_timers import ManualTimers
from base.lm.harness.codex.tests.fake_app_server import CODEX_PID
from base.lm.harness.codex.tests.support import start
from base.lm.harness.events import TurnCompleted

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
        with pytest.raises(RuntimeError, match="process"):
            await harness.session.close(reason="residue test")
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


async def test_default_turn_can_finish_after_two_quiet_days(tmp_path: Path) -> None:
    timers = ManualTimers()
    harness = await start(tmp_path, clock=clock_over(timers))
    try:
        ticket = harness.session.submit("wait for the provider")
        await harness.turn_started()
        timers.advance(2 * 24 * 3600)
        await asyncio.sleep(0)
        assert "turn/interrupt" not in harness.server.methods()
        assert not ticket.settled
        harness.server.complete()
        assert not (await asyncio.wait_for(ticket.outcome(), timeout=2)).is_error
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
