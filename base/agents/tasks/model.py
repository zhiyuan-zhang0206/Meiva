"""Shared SDK task dataclass, column order and immutable snapshot validation."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields

from pydantic import TypeAdapter

from base.agents.tasks.priority import DEFAULT_PRIORITY, validate_priority
from base.agents.tasks.status import TaskStatus
from base.agents.tasks.timestamps import render_task_timestamps


# meiva:note
# `remind_interval_seconds` is seconds without a change to the work before the owner is reminded.
# Reminders cannot be turned off, but an owner may silence them until an explicit, policy-capped
# deadline (`reminders_paused_until`); past the deadline they resume where they stood (task #5311).
@dataclass
class Task:
    id: int
    parent_id: int | None
    title: str
    description: str
    results: str | None
    status: str
    owner: int | None
    created_by: str
    created_at: str
    updated_at: str
    remind_interval_seconds: int | None = None
    last_reminded_at: str | None = None
    reminder_count: int = 0
    priority: str = DEFAULT_PRIORITY
    reminders_paused_until: str | None = None

    def __str__(self) -> str:
        owner = f"owner=#{self.owner}" if self.owner is not None else "unowned"
        parent = f" parent=#{self.parent_id}" if self.parent_id is not None else ""
        return f"#{self.id} [{self.status}] {self.title}  {owner}{parent}"


TASK_COLUMNS = ", ".join(field.name for field in fields(Task))


_TASK_COLUMN_NAMES = tuple(TASK_COLUMNS.split(", "))
_TASK = TypeAdapter(Task)


def task_from_row(row: tuple[object, ...]) -> Task:
    values = render_task_timestamps(row, TASK_COLUMNS)
    return _TASK.validate_python(dict(zip(_TASK_COLUMN_NAMES, values, strict=True)))


# meiva:note
# Validate every dataclass field without silently filling missing defaults, and hand back the `Task`.
def parse_task_snapshot(snapshot: dict[str, object]) -> Task:
    if set(snapshot) != {field.name for field in fields(Task)}:
        raise ValueError("task creation snapshot has missing or unknown fields")
    task = _TASK.validate_json(json.dumps(snapshot), strict=True)
    try:
        TaskStatus(task.status)
    except ValueError:
        raise ValueError(
            f"status must be one of {sorted(s.value for s in TaskStatus)}, got {task.status!r}"
        ) from None
    validate_priority(task.priority)
    return task


def validate_task_snapshot(snapshot: dict[str, object]) -> dict[str, object]:
    """Validate every dataclass field without silently filling missing defaults."""
    return asdict(parse_task_snapshot(snapshot))
