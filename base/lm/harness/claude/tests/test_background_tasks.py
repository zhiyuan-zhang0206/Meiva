from __future__ import annotations

import asyncio
from pathlib import Path

from base.lm.harness.claude.tests import fake_sdk as fake
from base.lm.harness.claude.tests.fake_sdk import Emit, Say
from base.lm.harness.claude.tests.support import start
from base.lm.harness.events import (
    SessionFailed,
    TurnCompleted,
    TurnFaulted,
    UnsolicitedOutput,
)


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


async def test_an_idle_task_report_starts_the_dispatch_hold(tmp_path: Path) -> None:
    delivered: list[UnsolicitedOutput] = []
    harness = await start(tmp_path, unsolicited=delivered.append)
    try:
        harness.client.push(_report())
        report = await harness.wait_for(
            lambda event: isinstance(event, UnsolicitedOutput) and "task-1" in event.text
        )
        assert report.source == "task-notification"
        assert "/var/tasks/task-1.output" in report.text
        assert "completed" in report.text

        # meiva:note
        # The report is inbound content: the agent has not read it yet, so it goes to
        # the sink that turns it into an inbound turn.
        # meiva:link base/lm/harness/claude/docs/background-tasks.meiva.okf.md
        assert [event.text for event in delivered] == [report.text]

        # meiva:note
        # Dispatch is held while the CLI answers the notification itself, so that
        # answer cannot be collected into a turn of ours.
        # meiva:link base/lm/harness/claude/docs/background-tasks.meiva.okf.md
        queued = harness.session.submit("next instruction")
        assert harness.session.active_turn_id is None
        assert harness.session.pending_turns == (queued.turn_id,)

        harness.client.push(_answer())
        completed = await harness.wait_for(
            lambda event: isinstance(event, TurnCompleted) and event.turn_id == queued.turn_id
        )
        assert completed.text == "" or isinstance(completed.text, str)
        assert harness.session.active_turn_id is None

        # meiva:note
        # The CLI's own answer belongs to no turn of ours and is not re-delivered as
        # new inbound content: the model already wrote it in the provider transcript.
        # meiva:link base/lm/harness/claude/docs/background-tasks.meiva.okf.md
        assert [event.text for event in delivered] == [report.text]
        assert harness.of(SessionFailed) == [] and harness.of(TurnFaulted) == []
    finally:
        await harness.stop()


async def test_a_foreign_result_does_not_close_the_running_turn(tmp_path: Path) -> None:
    harness = await start(
        tmp_path,
        scripts=[[Emit(_answer("not ours")), Say("ours")]],
    )
    try:
        outcome = await harness.session.submit("work").outcome()
        assert outcome.text == "ours"
        [completed] = harness.of(TurnCompleted)
        assert completed.text == "ours"
        assert [event.text for event in harness.of(UnsolicitedOutput)] == ["not ours"]
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
        outcome = await harness.session.submit("work").outcome()
        assert outcome.text == "saw it mid-turn"
        # meiva:note
        # The running turn received the notification in its own context; a second
        # delivery would be a duplicate turn.
        # meiva:link base/lm/harness/claude/docs/background-tasks.meiva.okf.md
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


async def test_an_idle_report_is_not_replayed_into_the_next_turn(tmp_path: Path) -> None:
    # meiva:note
    # Nobody consumes the event stream between turns, so an event that arrived while
    # the session was idle waits in the queue. The next turn must start from a clean
    # one: left there, a report or a failure that belonged to no turn is read as that
    # turn's own outcome (#5663).
    # meiva:link base/lm/harness/claude/docs/claude.meiva.okf.md
    harness = await start(tmp_path, scripts=[[Say("its own reply")]])
    try:
        await harness.detach_consumer()
        harness.client.push(_report("t1"))
        await asyncio.sleep(0.02)
        ticket = harness.session.submit("next")
        harness.client.push(_answer())
        outcome = await ticket.outcome()
        assert outcome.text == "its own reply"
        assert harness.session.unconsumed_events >= 1, "the idle period's events are not replayed"
        assert "task-1" not in outcome.text
    finally:
        await harness.stop()
