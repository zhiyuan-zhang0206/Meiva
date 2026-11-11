from __future__ import annotations

from pathlib import Path

from base.lm.harness.claude.tests import fake_sdk as fake
from base.lm.harness.claude.tests.fake_sdk import Emit, Say
from base.lm.harness.claude.tests.support import start
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


async def test_a_background_answer_is_an_observed_turn_with_its_cause(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        harness.client.push(_report())
        harness.client.push(
            fake.AssistantMessage(content=[fake.TextBlock(text="The command finished.")])
        )
        harness.client.push(_answer())
        completed = await harness.wait_for(lambda event: isinstance(event, TurnCompleted))
        [started] = harness.of(TurnStarted)
        assert started.origin is TurnOrigin.HARNESS
        assert started.cause is HarnessCause.BACKGROUND_TASK
        assert completed.turn_id == started.turn_id
        assert [event.turn_id for event in harness.of(AssistantText)] == [started.turn_id]
        assert harness.of(SessionFailed) == [] and harness.of(TurnFaulted) == []
        late = await send(harness, "ours")
        assert (await late.outcome()).turn_id == late.turn_id
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
