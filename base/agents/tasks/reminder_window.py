# meiva:note
# Which task writes restart a task's reminder ladder — one vocabulary for every write surface.
# `task_maintenance` counts consecutive unanswered reminders in `agent_tasks.reminder_count` and
# escalates once the count reaches Fleet `task_escalate_n`; a write makes those reminders stale
# only when it changes the work. Resetting on every write is what let the daily note stream (87%
# of all task updates) hold the ladder at zero, so no task ever passed one reminder (task #5314).
# meiva:link meiva_builtins/plugins/meiva_fleet/docs/tasks/task_model/task_model_reminders.meiva.okf.md

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal

# meiva:note
# Every way one update can change a task. `note` is not a column: a note appends a timestamped
# line to `results` as a log entry, so it is its own kind.
UpdateKind = Literal[
    "status",
    "title",
    "description",
    "results",
    "owner",
    "priority",
    "parent",
    "remind_interval_seconds",
    "note",
]

UPDATE_KINDS: frozenset[UpdateKind] = frozenset(
    {
        "status",
        "title",
        "description",
        "results",
        "owner",
        "priority",
        "parent",
        "remind_interval_seconds",
        "note",
    }
)

# meiva:note
# The work itself, not the record of it: a fresh reminder window starts when one of these
# changes. Title, description, interval and note restate or re-schedule the work, they do not
# advance it, so the default policy leaves them out.
DEFAULT_RESET_ON: tuple[UpdateKind, ...] = (
    "status",
    "results",
    "owner",
    "priority",
    "parent",
)


# meiva:note
# The task columns a write's change set is measured against, read from the row the writer locks.
@dataclass(frozen=True)
class TaskSnapshot:
    status: str
    title: str
    description: str | None
    results: str | None
    owner: int | None
    priority: str
    parent_id: int | None
    remind_interval_seconds: int | None


# meiva:note
# What one update asked to write. None means "leave alone", except `parent_id`, where None is the
# system root and `parent_written` says whether the caller passed it at all.
@dataclass(frozen=True)
class UpdateWrite:
    status: str | None = None
    title: str | None = None
    description: str | None = None
    results: str | None = None
    owner: int | None = None
    priority: str | None = None
    parent_id: int | None = None
    parent_written: bool = False
    remind_interval_seconds: int | None = None
    note: str | None = None


# meiva:note
# Free-text kinds: a blank value clears the field, which records nothing about the work.
_TEXT_KINDS: frozenset[UpdateKind] = frozenset({"description", "results"})


# meiva:note
# (kind, written value, current value) for every kind this write asked to change. A passed value
# is the only candidate, so an omitted field can never look like a change.
def _written_pairs(
    old: TaskSnapshot, write: UpdateWrite
) -> tuple[tuple[UpdateKind, object, object], ...]:
    candidates: tuple[tuple[UpdateKind, object, object], ...] = (
        ("status", write.status, old.status),
        ("title", write.title, old.title),
        ("description", write.description, old.description),
        ("results", write.results, old.results),
        ("owner", write.owner, old.owner),
        ("priority", write.priority, old.priority),
        ("remind_interval_seconds", write.remind_interval_seconds, old.remind_interval_seconds),
    )
    pairs: list[tuple[UpdateKind, object, object]] = [
        candidate for candidate in candidates if candidate[1] is not None
    ]
    if write.parent_written:
        pairs.append(("parent", write.parent_id, old.parent_id))
    if write.note is not None:
        pairs.append(("note", write.note, None))
    return tuple(pairs)


# meiva:note
# Blank text says nothing about the work, a restatement repeats it, and an appended note is always
# new text (it never restates what the task already says).
def _differs(kind: UpdateKind, written: object, current: object) -> bool:
    if kind == "note":
        return True
    if kind in _TEXT_KINDS and not str(written).strip():
        return False
    return written != current


# meiva:note
# The kinds this write actually changes relative to `old`. Repeating the current value is not a
# change: agents routinely restate `status` and `results`, and a restatement must not look like
# progress.
def changed_kinds(old: TaskSnapshot, write: UpdateWrite) -> frozenset[UpdateKind]:
    return frozenset(
        kind
        for kind, written, current in _written_pairs(old, write)
        if _differs(kind, written, current)
    )


# meiva:note
# True when any kind this write changed is one the policy restarts the window for.
def resets_window(changed: frozenset[UpdateKind], reset_on: Collection[str]) -> bool:
    return not changed.isdisjoint(reset_on)


# meiva:note
# Reject a policy naming an unknown kind, or naming nothing at all: an empty policy would let a
# task escalate once and then stay escalated forever, because nothing re-arms `escalated_at`.
def validate_reset_on(kinds: tuple[str, ...]) -> tuple[str, ...]:
    unknown = sorted(set(kinds) - UPDATE_KINDS)
    if unknown:
        raise ValueError(f"unknown update kinds {unknown}; known kinds are {sorted(UPDATE_KINDS)}")
    if not kinds:
        raise ValueError("the reminder window needs at least one kind that restarts it")
    return kinds
