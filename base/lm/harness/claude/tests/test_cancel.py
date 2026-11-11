from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from base.lm.harness.claude.tests import fake_sdk as fake
from base.lm.harness.claude.tests.fake_sdk import Block, Say
from base.lm.harness.claude.tests.support import CLI_PID, start
from base.lm.harness.delivery.tests.sent import send
from base.lm.harness.events import TurnCompleted

TOOL_MARKER = "ping -c 40 127.0.0.1"


async def test_interrupt_asserts_the_tool_children_are_reaped(tmp_path: Path) -> None:
    harness = await start(tmp_path, scripts=[[Block()]])
    try:
        ticket = await send(harness, "run the long command")
        await asyncio.sleep(0.01)
        harness.probe.spawn(5150, TOOL_MARKER)
        harness.client.on_interrupt = lambda: harness.probe.reap(5150)
        report = await harness.session.interrupt()
        assert report.turn_id == ticket.turn_id
        assert report.settled is True
        assert report.remaining == ()
        assert [observation.pid for observation in report.observed] == [5150]
        assert report.cli_pid == CLI_PID
        assert report.cli_alive is True
        assert harness.client.interrupts == 1
        completed = await harness.wait_for(lambda event: isinstance(event, TurnCompleted))
        assert completed.interrupted is True
        assert completed.is_error is True
    finally:
        await harness.stop()


async def test_surviving_child_is_reported_as_residue(tmp_path: Path) -> None:
    harness = await start(tmp_path, scripts=[[Block()]], cancel_settle_seconds=0.1)
    try:
        await send(harness, "run the long command")
        await asyncio.sleep(0.01)
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


async def test_unobservable_cli_is_not_a_pass(tmp_path: Path) -> None:
    harness = await start(tmp_path, scripts=[[Block()]], observe_pid=False)
    try:
        await send(harness, "run something")
        await asyncio.sleep(0.01)
        report = await harness.session.interrupt()
        assert report.settled is None
        assert report.cli_pid is None
        assert report.cli_alive is None
    finally:
        await harness.stop()


async def test_processes_seen_before_the_turn_are_not_blamed(tmp_path: Path) -> None:
    harness = await start(tmp_path, scripts=[[Block()]])
    try:
        harness.probe.spawn(4000, "claude --version")
        await send(harness, "run something")
        await asyncio.sleep(0.01)
        harness.probe.spawn(5150, TOOL_MARKER)
        harness.client.on_interrupt = lambda: harness.probe.reap(5150)
        report = await harness.session.interrupt()
        assert report.settled is True
        assert [observation.pid for observation in report.observed] == [5150]
        harness.probe.reap(4000)
    finally:
        await harness.stop()


async def test_a_helper_the_cli_starts_mid_turn_is_neither_waited_for_nor_signalled(
    tmp_path: Path,
) -> None:
    harness = await start(tmp_path, scripts=[[Block()]], cancel_settle_seconds=0.5)
    try:
        await send(harness, "use a tool")
        await asyncio.sleep(0.01)
        harness.probe.spawn(5200, "npx some-mcp-server", tool=False)
        harness.probe.spawn(5150, TOOL_MARKER)
        harness.client.on_interrupt = lambda: harness.probe.reap(5150)
        report = await harness.session.interrupt()
        assert report.settled is True
        assert report.remaining == ()
        assert [observation.pid for observation in report.observed] == [5150]
        assert report.waited_ms < 500.0
        assert harness.probe.signals == []
        assert harness.probe.is_alive(5200)
        harness.probe.reap(5200)
    finally:
        await harness.stop()


async def test_a_background_command_the_cli_announced_is_not_the_turns_work(
    tmp_path: Path,
) -> None:
    harness = await start(tmp_path, scripts=[[Block()]], cancel_settle_seconds=0.5)
    try:
        await send(harness, "start a server, then run a command")
        await asyncio.sleep(0.01)
        harness.probe.spawn(6000, "npm run dev")
        harness.client.push(fake.TaskStartedMessage(subtype="task_started", data={}, task_id="t1"))
        await asyncio.sleep(0.01)
        harness.probe.spawn(5150, TOOL_MARKER)
        harness.client.on_interrupt = lambda: harness.probe.reap(5150)
        report = await harness.session.interrupt()
        assert report.settled is True
        assert [observation.pid for observation in report.observed] == [5150]
        assert harness.probe.signals == []
        assert harness.probe.is_alive(6000)
        harness.probe.reap(6000)
    finally:
        await harness.stop()


async def test_session_stays_usable_after_interrupt(tmp_path: Path) -> None:
    harness = await start(tmp_path, scripts=[[Block()], [Say("still alive")]])
    try:
        await send(harness, "run something")
        await asyncio.sleep(0.01)
        await harness.session.interrupt()
        outcome = await (await send(harness, "next one")).outcome()
        assert outcome.text == "still alive"
        assert harness.client.queries == ["run something", "next one"]
    finally:
        await harness.stop()
