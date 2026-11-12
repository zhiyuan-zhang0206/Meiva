from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from base.clock.tests.fakes import clock_over
from base.clock.tests.manual_timers import ManualTimers
from base.lm.harness.claude.session import ANSWER_WAIT_SECONDS
from base.lm.harness.claude.tests import fake_sdk as fake
from base.lm.harness.claude.tests.fake_sdk import Emit, Say
from base.lm.harness.claude.tests.support import Harness, replay, start
from base.lm.harness.delivery.tests.sent import send
from base.lm.harness.events import (
    AssistantText,
    HarnessCause,
    SessionFailed,
    TurnCompleted,
    TurnFaulted,
    TurnOrigin,
    TurnStarted,
    UnsolicitedOutput,
)
from base.lm.harness.registry import FileSessionRegistry
from base.lm.harness.session import BackgroundKind


def _report(task_id: str = "task-1") -> fake.TaskNotificationMessage:
    return fake.TaskNotificationMessage(
        subtype="task_notification",
        data={},
        task_id=task_id,
        status="completed",
        output_file=f"/var/tasks/{task_id}.output",
        summary='Background command "sleep 100; echo BGDONE" completed (exit code 0)',
    )


# meiva:note
# Recorded with a real CLI (fixtures/background_bash.json): a background command ended while the
# agent was idle, and the CLI read its own notification and answered it in a turn of its own. The
# notification is therefore recorded in the event stream and never delivered to the sink; delivering
# it too would hand the agent the same report twice and run a second turn.
# meiva:link base/lm/harness/claude/docs/background-tasks.md
async def test_an_idle_background_end_is_answered_by_the_cli_and_not_delivered(
    tmp_path: Path,
) -> None:
    delivered: list[UnsolicitedOutput] = []
    harness = await start(tmp_path, passive=True, unsolicited=delivered.append)
    try:
        launch = await send(harness, "run it", key="first")
        replay(harness.client, "background_bash", "launch", first="first")
        assert (await launch.outcome()).text == "STARTED"
        assert [task.description for task in harness.session.background_work().items] == [
            "sleep 12; echo BGDONE"
        ]
        replay(harness.client, "background_bash", "idle_report")
        replay(harness.client, "background_bash", "idle_turn")
        answer = await harness.wait_for(
            lambda event: isinstance(event, TurnCompleted) and event.turn_id != launch.turn_id
        )
        [_, woken] = harness.of(TurnStarted)
        assert (woken.origin, woken.cause) == (TurnOrigin.HARNESS, HarnessCause.BACKGROUND_TASK)
        assert answer.turn_id == woken.turn_id
        assert answer.text == "Background command completed. Exit code: 0. Output: `BGDONE`"
        assert [e.turn_id for e in harness.of(AssistantText) if e.text == answer.text] == [
            woken.turn_id
        ]
        [report] = harness.of(UnsolicitedOutput)
        assert "task-bg-1 reported completed" in report.text
        assert delivered == []
        assert not harness.session.background_work()
        assert harness.of(SessionFailed) == [] and harness.of(TurnFaulted) == []
    finally:
        await harness.stop()


async def test_a_notification_during_a_turn_is_not_delivered_again(tmp_path: Path) -> None:
    delivered: list[UnsolicitedOutput] = []
    harness = await start(
        tmp_path,
        scripts=[[Emit(_report()), Say("saw it mid-turn")]],
        unsolicited=delivered.append,
    )
    try:
        outcome = await (await send(harness, "work")).outcome()
        assert outcome.text == "saw it mid-turn"
        # meiva:note
        # The running turn received the notification in its own context; a second
        # delivery would be a duplicate turn.
        # meiva:link base/lm/harness/claude/docs/background-tasks.md
        assert delivered == []
        [event] = harness.of(UnsolicitedOutput)
        assert "task-1" in event.text
    finally:
        await harness.stop()


async def test_close_stops_the_tasks_the_cli_still_runs(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        harness.client.push(fake.TaskStartedMessage(subtype="task_started", data={}, task_id="t1"))
        harness.client.push(fake.TaskStartedMessage(subtype="task_started", data={}, task_id="t2"))
        harness.client.push(_report("t1"))
        await harness.wait_for(lambda event: isinstance(event, UnsolicitedOutput))
        await harness.session.close(reason="test done")
        assert harness.client.stopped == ["t2"]
    finally:
        await harness.stop()


async def _eventually(predicate: Callable[[], bool]) -> None:
    for _ in range(1000):
        if predicate():
            return
        await asyncio.sleep(0.002)
    raise AssertionError("condition not reached in time")


# meiva:note
# A Monitor is a background command to the CLI: it announces it with `TaskStarted` and ends it with
# `TaskUpdated` plus `TaskNotification`, so the task set already holds the session while one runs.
# Its per-line events are turns of the CLI's own with no Task* frame, and change nothing here.
# meiva:link base/lm/harness/claude/docs/background-tasks.md
async def test_a_monitor_holds_the_session_until_its_source_ends_or_is_stopped(
    tmp_path: Path,
) -> None:
    timers = ManualTimers()
    harness = await start(tmp_path, passive=True, clock=clock_over(timers))
    try:
        ticket = await send(harness, "arm two monitors", key="first")
        replay(harness.client, "monitor_background", "launch", first="first")
        await ticket.outcome()
        work = harness.session.background_work()
        assert [(item.kind, item.description) for item in work.items] == [
            (BackgroundKind.COMMAND, "A short"),
            (BackgroundKind.COMMAND, "B long"),
        ]

        replay(harness.client, "monitor_background", "event")
        await _eventually(lambda: len(harness.of(TurnCompleted)) == 2)
        assert len(harness.session.background_work().items) == 2

        replay(harness.client, "monitor_background", "ended")
        commands = lambda: [  # noqa: E731
            i for i in harness.session.background_work().items if i.kind is BackgroundKind.COMMAND
        ]
        await _eventually(lambda: len(commands()) == 1)
        [left] = commands()
        assert left.description == "B long"

        replay(harness.client, "monitor_background", "stopped")
        await _eventually(lambda: not commands())
        # meiva:note
        # This fixture stops after the monitor's end notification, so no CLI turn answers it here
        # and the wait lapses (the manual clock sleeps through it at once). A real CLI answers it
        # (background_ends.json).
        # meiva:link base/lm/harness/claude/docs/background-tasks.md
        await _eventually(lambda: not harness.session.background_work())
    finally:
        await harness.stop()


async def _launched(
    harness: Harness, fixture: str = "background_bash", group: str = "launch"
) -> str:
    launch = await send(harness, "run it", key="first")
    replay(harness.client, fixture, group, first="first")
    await launch.outcome()
    return launch.turn_id


async def _stored(registry: FileSessionRegistry) -> list[BackgroundKind]:
    stored = registry.load(7, harness="claude_code")
    assert stored is not None
    return [item.kind for item in stored.background]


# meiva:note
# Between the notification and the CLI's first frame of its own turn nothing else says work is
# pending (the task is reported, no observed turn exists yet), and the idle TTL could recycle the
# session before the CLI answers. The session claims the wait itself, hands over to the observed turn
# item once the turn opens without counting it twice, and gives the report to the sink after
# `ANSWER_WAIT_SECONDS` if no turn answered it.
# meiva:link base/lm/harness/claude/docs/background-tasks.md
async def test_the_wait_for_the_clis_answer_counts_as_work_and_hands_over_to_its_turn(
    tmp_path: Path,
) -> None:
    delivered: list[UnsolicitedOutput] = []
    registry = FileSessionRegistry(tmp_path / "registry")
    harness = await start(
        tmp_path, passive=True, unsolicited=delivered.append, registry=registry, agent_id=7
    )
    try:
        launched = await _launched(harness)
        replay(harness.client, "background_bash", "idle_report")
        await harness.wait_for(lambda event: isinstance(event, UnsolicitedOutput))
        [waiting] = harness.session.background_work().items
        assert waiting.kind is BackgroundKind.TURN and "BGDONE" in waiting.description
        assert await _stored(registry) == [BackgroundKind.TURN]
        replay(harness.client, "background_bash", "idle_turn")
        await harness.wait_for(
            lambda event: isinstance(event, TurnCompleted) and event.turn_id != launched
        )
        assert not harness.session.background_work()
        assert await _stored(registry) == []
        assert delivered == []
    finally:
        await harness.stop()


async def test_a_turn_in_progress_replaces_the_wait_without_double_counting(
    tmp_path: Path,
) -> None:
    harness = await start(tmp_path, passive=True)
    try:
        await _launched(harness)
        replay(harness.client, "background_bash", "idle_report")
        harness.client.push(fake.AssistantMessage(content=[fake.TextBlock(text="reading it")]))
        await harness.wait_for(
            lambda event: (
                isinstance(event, TurnStarted) and event.cause is HarnessCause.BACKGROUND_TASK
            )
        )
        [running] = harness.session.background_work().items
        assert running.kind is BackgroundKind.TURN and "started by itself" in running.description
    finally:
        await harness.stop()


# meiva:note
# Recorded (fixtures/background_ends.json): a failed command and a Monitor that ended by itself
# while the session was idle were answered by the CLI exactly like a finished command, so their
# reports are not delivered either.
# meiva:link base/lm/harness/claude/docs/background-tasks.md
async def _answered_by_the_cli(
    tmp_path: Path, kind: str, launch: str, between: tuple[str, ...]
) -> None:
    delivered: list[UnsolicitedOutput] = []
    harness = await start(tmp_path, passive=True, unsolicited=delivered.append)
    try:
        launched = await _launched(harness, "background_ends", launch)
        for group in between:
            replay(harness.client, "background_ends", group)
        replay(harness.client, "background_ends", f"{kind}_report")
        await harness.wait_for(lambda event: isinstance(event, UnsolicitedOutput))
        assert harness.session.background_work()
        replay(harness.client, "background_ends", f"{kind}_turn")
        done = await harness.wait_for(
            lambda event: (
                isinstance(event, TurnCompleted)
                and event.turn_id != launched
                and "completed" in event.text.lower()
            )
        )
        started = next(e for e in harness.of(TurnStarted) if e.turn_id == done.turn_id)
        assert started.cause is HarnessCause.BACKGROUND_TASK
        assert not harness.session.background_work()
        assert delivered == []
    finally:
        await harness.stop()


async def test_a_failed_command_is_answered_by_the_cli(tmp_path: Path) -> None:
    await _answered_by_the_cli(tmp_path, "failed", "failed_launch", ())


async def test_a_monitor_that_ended_by_itself_is_answered_by_the_cli(tmp_path: Path) -> None:
    await _answered_by_the_cli(tmp_path, "monitor", "monitor_launch", ("monitor_event",))


async def test_a_stop_inside_our_turn_waits_for_nothing(tmp_path: Path) -> None:
    delivered: list[UnsolicitedOutput] = []
    harness = await start(tmp_path, passive=True, unsolicited=delivered.append)
    try:
        await _launched(harness, "background_ends", "stop_turn")
        assert not harness.session.background_work()
        assert [e.text for e in harness.of(UnsolicitedOutput)] == [
            "Background task task-s reported stopped.\nsleep 120"
        ]
        assert delivered == []
    finally:
        await harness.stop()


# meiva:note
# The wait ends only for the turn that answers the notification: a turn of ours that starts first
# (here the follow-up input of the recording) does not end it, because the CLI may have read the
# notification into that turn's context or not (a task stopped while idle was recorded as folded in
# silently), and the report is still owed to the agent when the wait lapses.
# meiva:link base/lm/harness/claude/docs/background-tasks.md
async def test_a_turn_of_ours_does_not_end_the_wait_for_an_unanswered_notification(
    tmp_path: Path,
) -> None:
    delivered: list[UnsolicitedOutput] = []
    harness = await start(tmp_path, passive=True, unsolicited=delivered.append)
    try:
        await _launched(harness, "background_ends", "stop_idle_launch")
        replay(harness.client, "background_ends", "stop_idle_report")
        await harness.wait_for(lambda event: isinstance(event, UnsolicitedOutput))
        follow = await send(harness, "what happened?", key="next")
        replay(harness.client, "background_ends", "stop_idle_followup", next="next")
        await follow.outcome()
        [waiting] = harness.session.background_work().items
        assert waiting.kind is BackgroundKind.TURN and "sleep 120" in waiting.description
        assert delivered == []
    finally:
        await harness.stop()


async def test_a_second_notification_keeps_its_own_wait_until_its_own_turn(
    tmp_path: Path,
) -> None:
    delivered: list[UnsolicitedOutput] = []
    harness = await start(tmp_path, passive=True, unsolicited=delivered.append)
    try:
        await _launched(harness)
        replay(harness.client, "background_bash", "idle_report")
        replay(harness.client, "background_ends", "failed_report")
        await _eventually(lambda: len(harness.of(UnsolicitedOutput)) == 2)
        assert len(harness.session.background_work().items) == 2
        replay(harness.client, "background_bash", "idle_turn")
        await _eventually(lambda: len(harness.of(TurnCompleted)) == 2)
        [left] = harness.session.background_work().items
        assert "exit 3" in left.description
        replay(harness.client, "background_ends", "failed_turn")
        await _eventually(lambda: not harness.session.background_work())
        assert delivered == []
    finally:
        await harness.stop()


# meiva:note
# When the wait lapses with no turn opened for the report, the report goes to the sink: whatever the
# CLI did (a stop notification it never answered, a behaviour a later version changes) the agent
# is not left without it. The manual clock sleeps through the wait at once. Delivery empties the
# pending record, so nothing is told twice.
# meiva:link base/lm/harness/claude/docs/background-tasks.md
async def test_a_report_nobody_answered_goes_to_the_sink_when_the_wait_lapses(
    tmp_path: Path,
) -> None:
    delivered: list[UnsolicitedOutput] = []
    registry = FileSessionRegistry(tmp_path / "registry")
    timers = ManualTimers()
    harness = await start(
        tmp_path,
        passive=True,
        unsolicited=delivered.append,
        registry=registry,
        agent_id=7,
        clock=clock_over(timers),
    )
    try:
        await _launched(harness, "background_ends", "stop_idle_launch")
        replay(harness.client, "background_ends", "stop_idle_report")
        await _eventually(lambda: bool(delivered))
        [report] = harness.of(UnsolicitedOutput)
        assert delivered == [report] and "stopped" in report.text
        assert ANSWER_WAIT_SECONDS in timers.sleeps
        assert not harness.session.background_work()
        assert await _stored(registry) == []
        await asyncio.sleep(0.02)
        assert delivered == [report]
    finally:
        await harness.stop()


# meiva:note
# A process lost inside the wait is not delivered to the sink by the next one (it sets no timer):
# the next session tells the agent once what was pending, and the record is then cleared.
# meiva:link base/lm/harness/claude/docs/background-tasks.md
async def test_a_restart_inside_the_wait_tells_the_agent_once_and_does_not_redeliver(
    tmp_path: Path,
) -> None:
    delivered: list[UnsolicitedOutput] = []
    registry = FileSessionRegistry(tmp_path / "registry")
    first = await start(
        tmp_path, passive=True, unsolicited=delivered.append, registry=registry, agent_id=7
    )
    await _launched(first, "background_ends", "failed_launch")
    replay(first.client, "background_ends", "failed_report")
    await first.wait_for(lambda event: isinstance(event, UnsolicitedOutput))
    await first.stop()
    second = await start(
        tmp_path,
        passive=True,
        unsolicited=delivered.append,
        registry=registry,
        agent_id=7,
        session_id="session-2",
    )
    try:
        [lost] = second.session.take_lost_background()
        assert lost.kind is BackgroundKind.TURN and "exit code 3" in lost.description
        assert second.session.take_lost_background() == ()
        assert not second.session.background_work()
        assert delivered == []
    finally:
        await second.stop()
