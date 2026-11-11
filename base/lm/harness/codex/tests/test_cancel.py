from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from base.clock.tests.fakes import clock_over
from base.clock.tests.manual_timers import ManualTimers
from base.lm.harness.codex.tests.fake_app_server import CODEX_PID
from base.lm.harness.codex.tests.support import start
from base.lm.harness.delivery.tests.sent import send
from base.lm.harness.events import TurnCompleted
from base.lm.harness.process import SIGKILL, SIGTERM

TOOL_MARKER = "sleep 40"


async def test_interrupt_ends_the_command_the_turn_started(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        ticket = await send(harness, "run the long command")
        await harness.turn_started()
        harness.server.command_started(command=TOOL_MARKER)
        harness.probe.spawn(5150, TOOL_MARKER)
        report = await harness.session.interrupt()
        harness.server.complete(status="interrupted")
        assert report.turn_id == ticket.turn_id
        assert harness.probe.signals == [(5150, SIGTERM)]
        assert not harness.probe.is_alive(5150)
        assert report.settled is True
        assert report.killed == ()
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


async def test_a_command_that_ignores_sigterm_is_killed(tmp_path: Path) -> None:
    harness = await start(tmp_path, cancel_settle_seconds=0.1)
    try:
        await send(harness, "run the long command")
        await harness.turn_started()
        harness.probe.spawn(5150, TOOL_MARKER)
        harness.probe.ignores_sigterm.add(5150)
        report = await harness.session.interrupt()
        assert harness.probe.signals == [(5150, SIGTERM), (5150, SIGKILL)]
        assert report.killed == (5150,)
        assert report.settled is True
    finally:
        await harness.stop()


async def test_a_command_that_survives_sigkill_is_reported_as_residue(tmp_path: Path) -> None:
    harness = await start(tmp_path, cancel_settle_seconds=0.1)
    try:
        await send(harness, "run the long command")
        await harness.turn_started()
        harness.probe.spawn(5150, TOOL_MARKER)
        harness.probe.immortal.add(5150)
        report = await harness.session.interrupt()
        assert report.settled is False
        assert [observation.pid for observation in report.remaining] == [5150]
        assert report.remaining[0].command == TOOL_MARKER
        assert report.waited_ms >= 100.0
        with pytest.raises(RuntimeError, match="process"):
            await harness.session.close(reason="residue test")
    finally:
        await harness.stop()


async def test_the_app_servers_own_helpers_are_neither_waited_for_nor_ended(
    tmp_path: Path,
) -> None:
    harness = await start(tmp_path, cancel_settle_seconds=0.1)
    try:
        await send(harness, "run the long command")
        await harness.turn_started()
        harness.probe.spawn(5150, TOOL_MARKER)
        harness.probe.spawn(5200, "codex-code-mode-host", tool=False)
        report = await harness.session.interrupt()
        assert harness.probe.signals == [(5150, SIGTERM)]
        assert harness.probe.is_alive(5200)
        assert report.settled is True
        assert [observation.pid for observation in report.observed] == [5150]
        assert report.waited_ms < 100.0
        harness.probe.reap(5200)
    finally:
        await harness.stop()


async def test_a_background_command_of_an_earlier_turn_is_left_running(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        first = await send(harness, "start a server")
        await harness.turn_started(1)
        harness.server.command_started(command="npm run dev")
        harness.probe.spawn(5001, "npm run dev")
        harness.server.complete()
        await asyncio.wait_for(first.outcome(), 2)
        assert harness.session.background_work().items

        await send(harness, "now run the long command")
        await harness.turn_started(2)
        harness.probe.spawn(5150, TOOL_MARKER)
        report = await harness.session.interrupt()
        harness.server.complete(status="interrupted")
        assert harness.probe.signals == [(5150, SIGTERM)]
        assert harness.probe.is_alive(5001)
        assert report.settled is True
        assert [observation.pid for observation in report.observed] == [5150]
        assert harness.session.background_work().items
        harness.probe.reap(5001)
    finally:
        await harness.stop()


async def test_unobservable_app_server_is_not_a_pass(tmp_path: Path) -> None:
    harness = await start(tmp_path, observe_pid=False)
    try:
        await send(harness, "run something")
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
        ticket = await send(harness, "wait for the provider")
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
        await send(harness, "run something")
        await harness.turn_started()
        await harness.session.interrupt()
        harness.server.complete(status="interrupted")
        await harness.wait_for(lambda event: isinstance(event, TurnCompleted))
        second = await send(harness, "next one")
        await harness.turn_started(2)
        harness.server.message("still alive")
        harness.server.complete()
        assert (await asyncio.wait_for(second.outcome(), 2)).text == "still alive"
    finally:
        await harness.stop()
