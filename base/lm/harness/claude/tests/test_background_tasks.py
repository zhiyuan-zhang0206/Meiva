from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from base.lm.harness.claude.tests import fake_sdk as fake
from base.lm.harness.claude.tests.fake_sdk import Emit, Say
from base.lm.harness.claude.tests.support import replay, start
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


def _answer(text: str = "The background command finished with exit code 0.") -> fake.ResultMessage:
    return fake.ResultMessage(origin={"kind": "task-notification"}, result=text)


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
        replay(harness.client, "background_bash", "idle_answer")
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
    harness = await start(tmp_path, passive=True)
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
        await _eventually(lambda: len(harness.session.background_work().items) == 1)
        [left] = harness.session.background_work().items
        assert left.description == "B long"

        replay(harness.client, "monitor_background", "stopped")
        await _eventually(lambda: not harness.session.background_work())
    finally:
        await harness.stop()
