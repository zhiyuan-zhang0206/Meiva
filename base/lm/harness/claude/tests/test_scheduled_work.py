from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from base.lm.harness.claude.inbound import (
    AFTER_DUE_MARGIN_SECONDS,
    CRON_EXPIRY_SECONDS,
    ScheduledWork,
)
from base.lm.harness.claude.tests import fake_sdk as fake
from base.lm.harness.claude.tests.fake_sdk import CallTool, Say
from base.lm.harness.claude.tests.support import start
from base.lm.harness.delivery import HarnessInput
from base.lm.harness.delivery.tests.sent import send
from base.lm.harness.events import (
    HarnessCause,
    TurnCompleted,
    TurnOrigin,
    TurnStarted,
    UnsolicitedOutput,
)
from base.lm.harness.registry import FileSessionRegistry
from base.lm.harness.session import BackgroundKind

_RECORDED: dict[str, Any] = json.loads(
    (Path(__file__).parent / "fixtures" / "scheduled_work.json").read_text()
)
NOW: float = _RECORDED["now"]


def _call(
    work: ScheduledWork, name: str, *, now: float = NOW, tool_use_id: str = "t"
) -> ScheduledWork:
    recorded = _RECORDED["calls"][name]
    work = work.tool_used(tool_use_id, recorded["name"], recorded["input"])
    return work.tool_returned(
        tool_use_id,
        content=recorded["content"],
        is_error=False,
        structured=recorded["structured"],
        now=now,
    )


def _kinds(work: ScheduledWork, now: float = NOW) -> list[BackgroundKind]:
    return [item.kind for item in work.pending_at(now)]


def test_a_recurring_cron_is_pending_until_it_is_deleted() -> None:
    work = _call(ScheduledWork(), "cron_recurring")
    assert _kinds(work) == [BackgroundKind.CRON]
    [item] = work.pending_at(NOW)
    assert "recurring cron `* * * * *`" in item.description
    assert "CRON-917 fired" in item.description

    assert _kinds(_call(work, "cron_delete", tool_use_id="d")) == []


def test_a_recurring_cron_expires_with_the_clis_own_seven_day_limit() -> None:
    work = _call(ScheduledWork(), "cron_recurring")
    assert _kinds(work, NOW + CRON_EXPIRY_SECONDS - 1) == [BackgroundKind.CRON]
    assert _kinds(work, NOW + CRON_EXPIRY_SECONDS + 1) == []


def test_a_one_shot_cron_stops_counting_shortly_after_its_fire_time() -> None:
    work = _call(ScheduledWork(), "cron_one_shot")
    fires = _RECORDED["calls"]["cron_one_shot"]["structured"]["nextFireAt"] / 1000
    assert _kinds(work, fires - 1) == [BackgroundKind.CRON]
    assert _kinds(work, fires + AFTER_DUE_MARGIN_SECONDS + 1) == []


async def test_a_one_shot_cron_with_no_structured_result_is_dated_from_its_expression(
    tmp_path: Path,
) -> None:
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("Asia/Shanghai")
    target = (datetime.now(zone) + timedelta(minutes=2)).replace(second=0, microsecond=0)
    cron = f"{target.minute} {target.hour} {target.day} {target.month} *"
    harness = await start(
        tmp_path,
        cron_timezone=zone,
        scripts=[
            [
                CallTool(
                    "CronCreate",
                    {"cron": cron, "prompt": "x", "recurring": False},
                    content="Scheduled one-shot task abcd1234 (x).",
                ),
                Say("SCHEDULED"),
            ]
        ],
    )
    try:
        _ = await (await send(harness, "set it")).outcome()
        [item] = harness.session.background_work().items
        assert item.until is not None
        assert abs(item.until - AFTER_DUE_MARGIN_SECONDS - target.timestamp()) < 1
    finally:
        await harness.stop()


async def test_a_one_shot_cron_with_an_unknown_cli_zone_is_not_dated(tmp_path: Path) -> None:
    call = CallTool(
        "CronCreate",
        {"cron": "0 0 1 1 *", "prompt": "x", "recurring": False},
        content="Scheduled one-shot task abcd1234 (x).",
    )
    harness = await start(tmp_path, scripts=[[call, Say("ok")]])
    try:
        _ = await (await send(harness, "set it")).outcome()
        [item] = harness.session.background_work().items
        assert item.until is not None and item.until > time.time() + 6 * 24 * 3600
    finally:
        await harness.stop()


def test_a_one_shot_cron_without_any_fire_time_keeps_the_seven_day_bound() -> None:
    work = ScheduledWork().tool_used("t", "CronCreate", {"cron": "bad", "recurring": False})
    work = work.tool_returned(
        "t",
        content="Scheduled one-shot task abcd1234 (x).",
        is_error=False,
        structured=None,
        now=NOW,
    )
    [item] = work.pending_at(NOW)
    assert item.until == NOW + CRON_EXPIRY_SECONDS


def test_a_wakeup_is_pending_until_its_due_time_and_stop_cancels_it() -> None:
    work = _call(ScheduledWork(), "wakeup")
    due = _RECORDED["calls"]["wakeup"]["structured"]["scheduledFor"] / 1000
    assert _kinds(work, NOW) == [BackgroundKind.WAKEUP]
    assert _kinds(work, due + AFTER_DUE_MARGIN_SECONDS + 1) == []

    assert _kinds(_call(work, "wakeup_stop", tool_use_id="s")) == []


def test_a_wakeup_without_a_structured_result_falls_back_to_the_result_text() -> None:
    recorded = _RECORDED["calls"]["wakeup"]
    work = ScheduledWork().tool_used("t", recorded["name"], recorded["input"])
    work = work.tool_returned(
        "t", content=recorded["content"], is_error=False, structured=None, now=NOW
    )
    assert _kinds(work, NOW + 70) == [BackgroundKind.WAKEUP]
    assert _kinds(work, NOW + 71 + AFTER_DUE_MARGIN_SECONDS + 1) == []


def test_a_failed_call_schedules_nothing() -> None:
    recorded = _RECORDED["calls"]["cron_recurring"]
    work = ScheduledWork().tool_used("t", recorded["name"], recorded["input"])
    work = work.tool_returned("t", content="bad cron", is_error=True, structured=None, now=NOW)
    assert work == ScheduledWork()


def test_an_unrecognised_create_result_still_counts_as_pending() -> None:
    recorded = _RECORDED["calls"]["cron_recurring"]
    work = ScheduledWork().tool_used("t", recorded["name"], recorded["input"])
    work = work.tool_returned(
        "t", content="a new wording", is_error=False, structured=None, now=NOW
    )
    assert _kinds(work) == [BackgroundKind.CRON]


def test_cron_list_replaces_the_cron_set_and_leaves_wakeups_alone() -> None:
    work = _call(ScheduledWork(), "cron_recurring", tool_use_id="a")
    work = _call(work, "wakeup", tool_use_id="w")
    work = _call(work, "cron_list", tool_use_id="l")

    descriptions = [item.description for item in work.pending_at(NOW)]
    assert any("status check every twenty minutes" in text for text in descriptions)
    assert not any("CRON-917" in text and "cron" in text for text in descriptions)
    assert sorted(_kinds(work)) == sorted([BackgroundKind.WAKEUP, BackgroundKind.CRON])


async def test_a_session_counts_the_timers_the_model_sets_and_persists_them(
    tmp_path: Path,
) -> None:
    recorded = _RECORDED["calls"]["cron_recurring"]
    registry = FileSessionRegistry(tmp_path / "registry")
    harness = await start(
        tmp_path,
        registry=registry,
        agent_id=7,
        scripts=[
            [
                CallTool(
                    recorded["name"],
                    recorded["input"],
                    tool_use_id="toolu-cron",
                    content=recorded["content"],
                    structured=recorded["structured"],
                ),
                Say("SCHEDULED"),
            ]
        ],
    )
    try:
        assert not harness.session.background_work()
        _ = await (await send(harness, "set a cron")).outcome()

        work = harness.session.background_work()
        assert [item.kind for item in work.items] == [BackgroundKind.CRON]

        stored = registry.load(7, harness="claude_code")
        assert stored is not None
        assert [item.kind for item in stored.background] == [BackgroundKind.CRON]
    finally:
        await harness.stop()


async def test_a_new_session_reads_back_what_the_old_process_left_pending(
    tmp_path: Path,
) -> None:
    recorded = _RECORDED["calls"]["cron_recurring"]
    registry = FileSessionRegistry(tmp_path / "registry")
    first = await start(
        tmp_path,
        registry=registry,
        agent_id=7,
        scripts=[
            [
                CallTool(
                    recorded["name"],
                    recorded["input"],
                    content=recorded["content"],
                    structured=recorded["structured"],
                ),
                Say("SCHEDULED"),
            ]
        ],
    )
    _ = await (await send(first, "set a cron")).outcome()
    await first.stop()

    second = await start(tmp_path, registry=registry, agent_id=7, session_id="session-2")
    try:
        assert not second.session.background_work()
        lost = second.session.take_lost_background()
        assert [item.kind for item in lost] == [BackgroundKind.CRON]
        assert second.session.take_lost_background() == ()
        stored = registry.load(7, harness="claude_code")
        assert stored is not None and stored.background == ()
    finally:
        await second.stop()


async def test_a_cleanly_finished_background_command_leaves_nothing_to_lose(
    tmp_path: Path,
) -> None:
    registry = FileSessionRegistry(tmp_path / "registry")
    harness = await start(tmp_path, registry=registry, agent_id=7)
    try:
        harness.client.push(
            fake.TaskStartedMessage(
                subtype="task_started",
                data={},
                task_id="bg-1",
                description="sleep 100; echo BGDONE",
                task_type="local_bash",
            )
        )
        await _eventually(lambda: bool(harness.session.background_work()))
        [item] = harness.session.background_work().items
        assert item.kind is BackgroundKind.COMMAND
        assert "sleep 100" in item.description
        stored = registry.load(7, harness="claude_code")
        assert stored is not None and len(stored.background) == 1

        harness.client.push(
            fake.TaskNotificationMessage(
                subtype="task_notification", data={}, task_id="bg-1", status="completed"
            )
        )
        await _eventually(lambda: not harness.session.background_work())
        stored = registry.load(7, harness="claude_code")
        assert stored is not None and stored.background == ()
    finally:
        await harness.stop()


async def test_a_turn_the_cli_starts_by_itself_is_an_observed_turn_not_an_unsolicited_report(
    tmp_path: Path,
) -> None:
    delivered: list[UnsolicitedOutput] = []
    harness = await start(tmp_path, unsolicited=delivered.append)
    try:
        # meiva:note
        # A fired cron: the CLI answers its own prompt with no turn of ours open.
        harness.client.push(fake.AssistantMessage(content=[fake.TextBlock(text="CRON-917 fired")]))
        harness.client.push(fake.ResultMessage(result="CRON-917 fired"))
        started = await harness.wait_for(
            lambda event: isinstance(event, TurnStarted) and event.origin is TurnOrigin.HARNESS
        )
        done = await harness.wait_for(lambda event: isinstance(event, TurnCompleted))
        assert isinstance(done, TurnCompleted) and done.turn_id == started.turn_id
        assert "CRON-917 fired" in done.text
        assert delivered == []
    finally:
        await harness.stop()


def test_a_due_wakeup_is_cleared_by_a_turn_the_cli_begins_itself() -> None:
    work = _call(ScheduledWork(), "wakeup")
    due = _RECORDED["calls"]["wakeup"]["structured"]["scheduledFor"] / 1000

    early, early_fired = work.fired(due - 30)
    assert early == work and not early_fired

    after, fired = work.fired(due + 1)
    assert fired and _kinds(after, due + 1) == []


def test_a_recurring_cron_explains_a_turn_but_stays() -> None:
    work = _call(ScheduledWork(), "cron_recurring")
    same, explains = work.fired(NOW + 60)
    assert same == work and explains


async def test_a_wake_turn_is_classified_and_clears_the_wakeup(tmp_path: Path) -> None:
    recorded = _RECORDED["calls"]["wakeup"]
    delivered: list[UnsolicitedOutput] = []
    harness = await start(
        tmp_path,
        unsolicited=delivered.append,
        scripts=[
            [
                CallTool(
                    recorded["name"],
                    {**recorded["input"], "delaySeconds": 0},
                    content=recorded["content"],
                    structured={"scheduledFor": (time.time() + 0.2) * 1000},
                ),
                Say("SCHEDULED"),
            ]
        ],
    )
    try:
        _ = await (await send(harness, "set a wakeup")).outcome()
        assert harness.session.background_work()
        await asyncio.sleep(0.5)

        harness.client.push(fake.AssistantMessage(content=[fake.TextBlock(text="WAKE fired")]))
        harness.client.push(fake.ResultMessage(result="WAKE fired"))
        started = await harness.wait_for(
            lambda event: isinstance(event, TurnStarted) and event.origin is TurnOrigin.HARNESS
        )
        assert started.cause is HarnessCause.SCHEDULED_WAKE
        await harness.wait_for(
            lambda event: isinstance(event, TurnCompleted) and event.turn_id == started.turn_id
        )
        assert not harness.session.background_work()
        assert delivered == []
    finally:
        await harness.stop()


async def test_a_turn_started_through_delivery_is_not_reported_as_the_clis_own(
    tmp_path: Path,
) -> None:
    delivered: list[UnsolicitedOutput] = []
    harness = await start(tmp_path, unsolicited=delivered.append, scripts=[[Say("created")]])
    try:
        await harness.session.deliver([HarnessInput(key="in-1", text="make it")])
        _ = await harness.wait_for(lambda event: isinstance(event, TurnCompleted))
        assert delivered == []
    finally:
        await harness.stop()


async def test_a_turn_the_cli_is_running_by_itself_counts_as_background_work(
    tmp_path: Path,
) -> None:
    harness = await start(tmp_path)
    try:
        harness.client.push(fake.AssistantMessage(content=[fake.TextBlock(text="working")]))
        await _eventually(lambda: bool(harness.session.background_work()))
        [item] = harness.session.background_work().items
        assert item.kind is BackgroundKind.TURN

        harness.client.push(fake.ResultMessage(result="working"))
        await _eventually(lambda: not harness.session.background_work())
    finally:
        await harness.stop()


async def _eventually(predicate: Any, *, timeout: float = 2.0) -> None:
    import asyncio

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.002)
    raise AssertionError("condition not reached in time")
