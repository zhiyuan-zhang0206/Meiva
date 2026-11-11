from __future__ import annotations

from pathlib import Path

from base.lm.harness.antigravity.schedules import (
    UNBOUNDED_SCHEDULE_SECONDS,
    Schedules,
)
from base.lm.harness.antigravity.tests.support import start
from base.lm.harness.events import ToolResult, ToolUse
from base.lm.harness.registry import FileSessionRegistry
from base.lm.harness.session import BackgroundKind

NOW = 1_794_329_400.0


# meiva:note
# No recording of the `schedule` tool's parameters exists; the parameter name `DurationSeconds` is the
# one the CLI's changelog names, and everything else about the call is treated as opaque.
# meiva:link base/lm/harness/antigravity/docs/antigravity.md
def _scheduled(params: dict[str, object], *, is_error: bool = False) -> Schedules:
    work = Schedules().tool_used("s1", "schedule", params)
    return work.tool_returned("s1", is_error=is_error, now=NOW)


def test_a_successful_schedule_call_is_pending_for_a_day_when_it_names_no_duration() -> None:
    work = _scheduled({"Cron": "*/5 * * * *", "Prompt": "check CI"})
    [item] = work.pending_at(NOW)
    assert item.kind is BackgroundKind.SCHEDULE
    assert "check CI" in item.description
    assert work.pending_at(NOW + UNBOUNDED_SCHEDULE_SECONDS - 1)
    assert not work.pending_at(NOW + UNBOUNDED_SCHEDULE_SECONDS + 1)


def test_a_timer_with_a_duration_adds_nothing_after_its_blocking_call_returns() -> None:
    for value in (120, "120"):
        work = _scheduled({"DurationSeconds": value, "Prompt": "ping"})
        assert not work.pending_at(NOW)


def test_a_failed_or_other_tool_call_schedules_nothing() -> None:
    assert not _scheduled({"Prompt": "x"}, is_error=True).pending_at(NOW)
    other = Schedules().tool_used("t", "run_command", {"x": 1})
    assert other == Schedules()


async def test_a_session_counts_schedules_and_loses_them_when_its_process_is_replaced(
    tmp_path: Path,
) -> None:
    registry = FileSessionRegistry(tmp_path / "registry")
    harness = await start(tmp_path, agent_id=7, registry=registry)
    try:
        assert not harness.session.background_work()
        harness.session._handle({"event": "init", "conversation_id": "conv-1", "init": {}})
        harness.session._track_schedule(
            ToolUse(turn_id="t", tool_use_id="s1", name="schedule", tool_input={"Prompt": "ping"})
        )
        harness.session._track_schedule(
            ToolResult(turn_id="t", tool_use_id="s1", is_error=False, content="ok")
        )
        [item] = harness.session.background_work().items
        assert item.kind is BackgroundKind.SCHEDULE
        stored = registry.load(7, harness="antigravity")
        assert stored is not None and len(stored.background) == 1

        harness.session._lose_schedules()

        assert not harness.session.background_work()
        assert [i.kind for i in harness.session.take_lost_background()] == [BackgroundKind.SCHEDULE]
    finally:
        await harness.stop()
