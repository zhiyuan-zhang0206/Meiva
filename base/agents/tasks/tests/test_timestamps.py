from datetime import UTC, datetime

import pytest

from base.agents.tasks.model import TASK_COLUMNS, task_from_row
from base.agents.tasks.timestamps import render_task_timestamps
from base.clock import Clock, ClockConfig
from base.clock.tests.manual_timers import ManualTimers


@pytest.mark.parametrize(
    ("timezone", "expected"),
    [("Asia/Shanghai", "[2026-11-06 08:00:00]"), ("UTC", "[2026-11-06 00:00:00]")],
)
def test_row_conversion_uses_its_owners_clock(timezone: str, expected: str) -> None:
    instant = datetime(2026, 11, 6, tzinfo=UTC)
    clock = Clock(ClockConfig(timezone, timezone, False), timers=ManualTimers())
    values = {
        "id": 1,
        "parent_id": 1,
        "title": "task",
        "description": "detail",
        "status": "in_progress",
        "owner": 2,
        "created_by": "user",
        "created_at": instant,
        "updated_at": instant,
        "priority": "P2",
        "reminder_count": 0,
    }
    row = tuple(values.get(name) for name in TASK_COLUMNS.split(", "))

    task = task_from_row(row, clock=clock)

    assert task.created_at == expected
    assert task.updated_at == expected
    assert task.last_reminded_at is None
    assert task.reminders_paused_until is None


def test_only_timestamp_columns_are_rendered() -> None:
    instant = datetime(2026, 11, 6, tzinfo=UTC)
    clock = Clock(ClockConfig("UTC", "UTC", False), timers=ManualTimers())

    result = render_task_timestamps(
        (instant, instant, None), "created_at, results, last_reminded_at", clock=clock
    )

    assert result == ("[2026-11-06 00:00:00]", instant, None)
