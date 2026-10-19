from __future__ import annotations

import contextlib
import os
import signal
from pathlib import Path

from base.lm.claude_sdk.events import TurnCompleted
from base.lm.harness.antigravity.tests.support import config, start
from base.lm.harness.antigravity.transport import AgyTransport


async def test_interrupt_asserts_the_tool_children_are_reaped(tmp_path: Path) -> None:
    turn = {"hang": True, "spawn_child": True, "kill_child_on_interrupt": True, "pace": 0.0}
    harness = await start(tmp_path, turns=[turn])
    try:
        ticket = harness.session.submit("run the long command")
        await harness.wait_until(lambda: harness.child_pid() is not None)
        report = await harness.session.interrupt()
        assert report.turn_id == ticket.turn_id
        assert report.settled is True
        assert report.remaining == ()
        assert [observation.pid for observation in report.observed] == [harness.child_pid()]
        assert report.cli_pid is not None
        assert report.cli_alive is False
        completed = await harness.wait_for(lambda event: isinstance(event, TurnCompleted))
        assert completed.interrupted is True
        assert completed.is_error is True
        assert completed.subtype == "interrupted"
    finally:
        await harness.stop()


async def test_a_surviving_child_is_reported_as_residue(tmp_path: Path) -> None:
    turn = {"hang": True, "spawn_child": True, "kill_child_on_interrupt": False}
    harness = await start(tmp_path, turns=[turn], cancel_settle_seconds=0.3)
    child = None
    try:
        harness.session.submit("run the long command")
        await harness.wait_until(lambda: harness.child_pid() is not None)
        child = harness.child_pid()
        report = await harness.session.interrupt()
        assert report.settled is False
        assert [observation.pid for observation in report.remaining] == [child]
    finally:
        if child is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(child, signal.SIGKILL)
        await harness.stop()


async def test_interrupt_without_an_active_turn_keeps_the_process(tmp_path: Path) -> None:
    from base.lm.harness.antigravity.tests.support import say

    harness = await start(tmp_path, turns=[say("one")])
    try:
        await harness.session.submit("a").outcome()
        report = await harness.session.interrupt()
        assert report.turn_id is None
        assert report.settled is None
        assert report.cli_alive is True
    finally:
        await harness.stop()


async def test_a_transport_that_never_started_cannot_pass(tmp_path: Path) -> None:
    transport = AgyTransport(config(tmp_path))
    report = await transport.interrupt(turn_id="turn-x")
    assert report.settled is None
    assert report.cli_pid is None
    assert report.cli_alive is None
