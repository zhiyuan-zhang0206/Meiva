from __future__ import annotations

import contextlib
import os
import signal
from collections.abc import Iterable, Mapping
from pathlib import Path

from base.lm.harness.antigravity.tests.support import config, start
from base.lm.harness.antigravity.transport import AgyTransport
from base.lm.harness.events import TurnCompleted


def _reap_nothing(observed: Mapping[int, str], pids: Iterable[int]) -> tuple[int, ...]:
    return ()


async def test_interrupt_reaps_the_tool_children_the_cli_leaves_behind(tmp_path: Path) -> None:
    # The real CLI exits on SIGINT and orphans its `run_command` tree; the fake
    # reproduces that by not killing the child itself.
    turn = {"hang": True, "spawn_child": True, "kill_child_on_interrupt": False}
    harness = await start(tmp_path, turns=[turn], cancel_settle_seconds=0.3)
    child = None
    try:
        ticket = harness.session.submit("run the long command")
        await harness.wait_until(lambda: harness.child_pid() is not None)
        child = harness.child_pid()
        report = await harness.session.interrupt()
        assert report.turn_id == ticket.turn_id
        assert report.killed == (child,)
        assert report.settled is True
        assert report.remaining == ()
        assert [observation.pid for observation in report.observed] == [child]
        assert report.cli_pid is not None
        assert report.cli_alive is False
        completed = await harness.wait_for(lambda event: isinstance(event, TurnCompleted))
        assert completed.interrupted is True
        assert completed.is_error is True
        assert completed.subtype == "interrupted"
    finally:
        if child is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(child, signal.SIGKILL)
        await harness.stop()


async def test_an_unkillable_child_is_still_reported_as_residue(tmp_path: Path) -> None:
    # A reaper that cannot kill stands in for a process we are not allowed to
    # signal: the report must stay honest instead of claiming a clean cancel.
    turn = {"hang": True, "spawn_child": True, "kill_child_on_interrupt": False}
    harness = await start(tmp_path, turns=[turn], cancel_settle_seconds=0.2, reap=_reap_nothing)
    child = None
    try:
        harness.session.submit("run the long command")
        await harness.wait_until(lambda: harness.child_pid() is not None)
        child = harness.child_pid()
        report = await harness.session.interrupt()
        assert report.killed == ()
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
        assert report.killed == ()
    finally:
        await harness.stop()


async def test_a_transport_that_never_started_cannot_pass(tmp_path: Path) -> None:
    transport = AgyTransport(config(tmp_path))
    report = await transport.interrupt(turn_id="turn-x")
    assert report.settled is None
    assert report.cli_pid is None
    assert report.cli_alive is None
