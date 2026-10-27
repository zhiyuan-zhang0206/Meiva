"""Task tracking — shared, persistent work items. Any agent can update any
task; owners are reminded periodically, and a task may nest under a parent.
"""

from __future__ import annotations

import builtins
from collections.abc import Iterable
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING, Any

import meiva
import meiva.sdk_surface.agent_identity
from base.agents.context import MeivaContext
from base.agents.tasks.model import TASK_COLUMNS, task_from_row
from base.agents.tasks.model import Task as Task
from base.agents.tasks.owner_notifications import TaskOwnerNotification, owner_change_notifications
from base.agents.tasks.reminder_window import UpdateWrite
from base.agents.tasks.reparent import resolve_reparent
from base.log import logger
from meiva.sdk_surface.validation import coerce_str, coerce_typed

if TYPE_CHECKING:
    # Annotation-only here (cursor params); the runtime import sits at the raise
    # site so plugin autoload stays off the psycopg stack (task #3816).
    import psycopg

    from base.events.live.bus import EventBus
    from base.telemetry import Event

from ._task_assignment import TaskAssignment, create_and_assign_guarded
from ._task_publication import TaskPublication
from ._task_requests import creation_request, effective_owner, update_request
from ._task_update import (
    DEFAULT_PRIORITY,
    TASK_STATUS_VALUES,
    UNSET_FIELD,
    Unset,
    collect_update_fields,
    nothing_to_update,
    owner_actually_changed,
    validate_status,
    write_task_update,
)

# `list` / `get` shadow builtins intentionally: these are the agent-facing names
# (meiva.tasks.list / meiva.tasks.get), matching meiva.shell.sessions.list. flake8-builtins
# (`A`) is not in this repo's ruff select, and the SDK renderer reads annotations
# as strings without evaluating them, so the shadow is runtime-cosmetic. It is
# NOT invisible to a type checker, though: within this module the name `list`
# resolves to the function, so annotations that mean the builtin container are
# spelled `builtins.list[...]`.
__all_for_meiva__ = [
    "Task",
    "TaskAssignment",
    "create",
    "create_and_assign",
    "get",
    "list",
    "log",
    "update",
]


def create(
    title: str,
    description: str,
    *,
    parent: int,
    remind_interval_seconds: int | None = None,
    owner: int | None = None,
    priority: str = DEFAULT_PRIORITY,
    operation_key: str,
) -> Task:
    """Args:
    title: unique among in_progress tasks.
    parent: id of an existing, open task this task descends from; the system
        root (id 1) parents only top-level tasks. An invalid parent raises ValueError.
    remind_interval_seconds: cannot be disabled; None = the priority default
        (P0 30m / P1 1h / P2 2h / P3 4h), capped at 24h.
    owner: agent to assign to; None means you.
    priority: "P0" (highest) through "P3" (lowest).
    operation_key: reuse the same key and inputs to return the original Task.
        The returned snapshot may be outdated; use get(task.id) for current state.
    """
    return create_in_context(
        meiva.context,
        title,
        description,
        parent=parent,
        remind_interval_seconds=remind_interval_seconds,
        owner=owner,
        priority=priority,
        operation_key=operation_key,
    )


# meiva:note
# The mutable SDK return value is separate from the immutable committed publication facts.
# Changing the Task cannot retarget a board refresh or alter the recorded audit events.
@dataclass
class CreationResult:
    task: Task
    publication: TaskPublication | None


# meiva:note
# Retain the canonical facts already written as JSONB, without keeping mutable Events.
# Serialization stays deferred so importing the plugin does not import the telemetry stack.
def _recorded_publication(
    actor: int, task_id: int, events: Iterable[Event], *, closed_title: str | None = None
) -> TaskPublication:
    from base.telemetry.delivery.serialization import event_line

    return TaskPublication(
        actor, task_id, tuple(event_line(event) for event in events), closed_title
    )


def publication_events(publication: TaskPublication) -> tuple[Event, ...]:
    from pydantic import TypeAdapter

    from base.telemetry.delivery.receipts import Event

    adapter = TypeAdapter(Event)
    return tuple(adapter.validate_json(line) for line in publication.event_lines)


def create_in_context(
    context: MeivaContext,
    title: str,
    description: str,
    *,
    parent: int,
    remind_interval_seconds: int | None = None,
    owner: int | None = None,
    priority: str = DEFAULT_PRIORITY,
    operation_key: str,
) -> Task:
    """Apply create with one explicit context, including admission revalidation."""
    from base.agents.tasks.priority import (
        DEFAULT_REMIND_INTERVAL_SECONDS,
        Priority,
        validate_priority,
    )

    priority = coerce_str(priority, "priority")
    validate_priority(priority)
    default_interval = DEFAULT_REMIND_INTERVAL_SECONDS[Priority(priority)]
    bus = context.require_bus()
    committed = commit_creation(
        context,
        title,
        description,
        default_remind_interval_seconds=default_interval,
        parent=parent,
        remind_interval_seconds=remind_interval_seconds,
        owner=owner,
        priority=priority,
        operation_key=operation_key,
    )
    publish_creation(committed.publication, bus)
    return committed.task


# meiva:note
# The transaction half of a creation: replay an earlier acceptance of the operation key, or insert
# the task with its audit fact, owner notifications and receipt, and commit them together. Nothing
# leaves the process here; a caller that stops after this has a committed task whose events were
# never emitted, which is the state a crash between commit and emission leaves.
def commit_creation(
    context: MeivaContext,
    title: str,
    description: str,
    *,
    parent: int,
    remind_interval_seconds: int | None = None,
    owner: int | None = None,
    priority: str = DEFAULT_PRIORITY,
    operation_key: str,
    default_remind_interval_seconds: int,
) -> CreationResult:
    from base.agents.tasks.creation import create_task_in_transaction
    from base.api_contracts.idempotency import validate_idempotency_key

    from ._task_creation_receipts import record_creation, replay_creation

    operation_key = validate_idempotency_key(operation_key)
    title = coerce_str(title, "title")
    description = coerce_str(description, "description")
    parent = coerce_typed(parent, "parent", int)
    remind_interval_seconds = coerce_typed(
        remind_interval_seconds, "remind_interval_seconds", int, allow_none=True
    )
    owner = coerce_typed(owner, "owner", int, allow_none=True)
    priority = coerce_str(priority, "priority")
    actor = meiva.sdk_surface.agent_identity.require_agent_id(context)
    task_owner = effective_owner(owner, actor)
    request = creation_request(
        title=title,
        description=description,
        parent=parent,
        owner=task_owner,
        priority=priority,
        remind_interval_seconds=remind_interval_seconds,
    )
    with context.sql.transaction(), context.sql.cursor() as cur:
        snapshot = replay_creation(cur, actor, operation_key, request, context=context)
        if snapshot is not None:
            return CreationResult(Task(**snapshot), None)
        task, created_event, note_events = create_task_in_transaction(
            cur,
            title,
            description,
            parent=parent,
            owner=task_owner,
            actor=actor,
            priority=priority,
            remind_interval_seconds=(
                default_remind_interval_seconds
                if remind_interval_seconds is None
                else remind_interval_seconds
            ),
        )
        record_creation(cur, actor, operation_key, request, asdict(task))
    return CreationResult(
        task, _recorded_publication(actor, task.id, (created_event, *note_events))
    )


# meiva:note
# The emission half of a creation: the recorded events, then a live refresh of every open task
# board. A replayed acceptance publishes nothing.
def publish_creation(committed: TaskPublication | None, bus: EventBus) -> None:
    if committed is None:
        return
    # meiva:note
    # Deferred imports keep the psycopg stack off plugin autoload (task #3816).
    from base import telemetry
    from base.events.live import announce

    for event in publication_events(committed):
        telemetry.emit_prepared(event)
    announce.publish_task_created_sync(bus, committed.actor, committed.task_id)


def create_and_assign(
    title: str,
    description: str,
    *,
    operation_key: str,
    label: str | None = None,
    config_overlay: dict[str, Any] | None = None,
    machine: str | None = None,
    parent: int,
    remind_interval_seconds: int | None = None,
    priority: str = DEFAULT_PRIORITY,
) -> TaskAssignment:
    """Create an agent and its assigned task under one operation key.

    Reuse the key and inputs to recover the original result after a lost reply.
    The result proves acceptance; query task and agent state to observe
    current progress. Borrowed leases are unsupported.

    Arguments have the same meaning as in create() and meiva.agents.spawn();
    machine defaults to your own machine. The parent must exist and be open before
    either object is born.

    Returns:
        A TaskAssignment; it unpacks as `task, agent_id`.
    """
    from base.api_contracts.idempotency import validate_idempotency_key

    operation_key = validate_idempotency_key(operation_key)
    title = coerce_str(title, "title")
    description = coerce_str(description, "description")
    label = coerce_str(label, "label", allow_none=True)
    config_overlay = coerce_typed(config_overlay, "config_overlay", dict, allow_none=True)
    machine = coerce_str(machine, "machine", allow_none=True)
    parent = coerce_typed(parent, "parent", int)
    remind_interval_seconds = coerce_typed(
        remind_interval_seconds, "remind_interval_seconds", int, allow_none=True
    )
    priority = coerce_str(priority, "priority")
    return create_and_assign_guarded(
        title,
        description,
        parent=parent,
        label=label,
        config_overlay=config_overlay,
        machine=machine,
        remind_interval_seconds=remind_interval_seconds,
        priority=priority,
        operation_key=operation_key,
    )


def update(
    task_id: int,
    *,
    status: str | None = None,
    title: str | None = None,
    description: str | None = None,
    results: str | None = None,
    owner: int | None = UNSET_FIELD,  # type: ignore[assignment]
    remind_interval_seconds: int | None = UNSET_FIELD,  # type: ignore[assignment]
    priority: str | None = None,
    parent_id: int | None = UNSET_FIELD,  # type: ignore[assignment]
    note: str | None = None,
    operation_key: str,
) -> None:
    """A write that changes the work restarts the task's reminder window; recording a
    note, or renaming the task, does not. Owner changes notify both owners; other
    updates tell the owner who changed it; a parent-only reparent stays silent.

    Args:
        status: one of "in_progress", "done", "cancelled"; closing is rejected
            while any direct child is in progress.
        results: replaces the whole field; use note to append instead.
        owner: agent id to reassign to; a task always has an owner.
        remind_interval_seconds: None = unchanged; reminders cannot be disabled; capped at 24h.
        parent_id: reparent (explicit None = system root; int = set parent).
        operation_key: required stable key for this agent and task; reuse it to
            replay a committed update without another note or notification.
            Different effective fields with the same key raise ValueError.
    """
    update_in_context(
        meiva.context,
        task_id,
        status=status,
        title=title,
        description=description,
        results=results,
        owner=owner,
        remind_interval_seconds=remind_interval_seconds,
        priority=priority,
        parent_id=parent_id,
        note=note,
        operation_key=operation_key,
    )


def _update_value(value: int | Unset | None) -> int | None:
    """Normalize the two unchanged spellings at typed business-call boundaries."""
    return None if isinstance(value, Unset) else value


# meiva:note
# The reminder-window policy is read from this home's `meiva_fleet` image on every write rather than
# cached for the process: an edit then takes effect at the next write, with no restart. It is
# `host`-scoped on purpose. A `cluster-pinned` read would overlay the gateway's boot packet, and an
# agent whose launcher sent a packet from the previous schema would fail every task update with
# SchemaDriftError — a rollout would break the fleet's reporting path. The imports are deferred so
# the config stack stays off plugin autoload (task #3816).
def _reminder_reset_on() -> frozenset[str]:
    """Kinds of task update that restart a task's reminder window, per this home's policy."""
    from base.packages.plugins.config_registration import disk_image_path, read_config_image

    from .default_config import FleetConfig

    return frozenset(
        read_config_image(FleetConfig, disk_image_path("meiva_fleet")).task_reminder_reset_on
    )


def update_in_context(
    context: MeivaContext,
    task_id: int,
    *,
    status: str | None = None,
    title: str | None = None,
    description: str | None = None,
    results: str | None = None,
    owner: int | Unset | None = UNSET_FIELD,
    remind_interval_seconds: int | Unset | None = UNSET_FIELD,
    priority: str | None = None,
    parent_id: int | Unset | None = UNSET_FIELD,
    note: str | None = None,
    operation_key: str,
) -> None:
    """Apply update with one explicit context, including admission revalidation."""
    bus = context.require_bus()
    publish_update(
        commit_update(
            context,
            task_id,
            status=status,
            title=title,
            description=description,
            results=results,
            owner=owner,
            remind_interval_seconds=remind_interval_seconds,
            priority=priority,
            parent_id=parent_id,
            note=note,
            operation_key=operation_key,
        ),
        bus,
    )


# meiva:note
# The transaction half of an update: replay an earlier commit of the operation key, or write the
# task row, its audit fact, the owner notifications and the receipt, and commit them together.
# Nothing leaves the process here; a caller that stops after this has a committed update whose
# events were never emitted, which is the state a crash between commit and emission leaves.
def commit_update(
    context: MeivaContext,
    task_id: int,
    *,
    status: str | None = None,
    title: str | None = None,
    description: str | None = None,
    results: str | None = None,
    owner: int | Unset | None = UNSET_FIELD,
    remind_interval_seconds: int | Unset | None = UNSET_FIELD,
    priority: str | None = None,
    parent_id: int | Unset | None = UNSET_FIELD,
    note: str | None = None,
    operation_key: str,
) -> TaskPublication | None:
    task_id = coerce_typed(task_id, "task_id", int)
    status = coerce_str(status, "status", allow_none=True)
    title = coerce_str(title, "title", allow_none=True)
    description = coerce_str(description, "description", allow_none=True)
    results = coerce_str(results, "results", allow_none=True)
    if owner is not UNSET_FIELD:
        owner = coerce_typed(owner, "owner", int, allow_none=True)
    if remind_interval_seconds is not UNSET_FIELD:
        remind_interval_seconds = coerce_typed(
            remind_interval_seconds, "remind_interval_seconds", int, allow_none=True
        )
    priority = coerce_str(priority, "priority", allow_none=True)
    if parent_id is not UNSET_FIELD:
        parent_id = coerce_typed(parent_id, "parent_id", int, allow_none=True)
    note = coerce_str(note, "note", allow_none=True)
    validate_status(status)

    sets, params, payload, owner_changing, changes = collect_update_fields(
        task_id,
        status,
        title,
        description,
        results,
        _update_value(owner),
        _update_value(remind_interval_seconds),
        priority,
    )
    if nothing_to_update(sets, note) and parent_id is UNSET_FIELD:
        raise ValueError(
            "update needs at least one of status, title, description, results, "
            "owner, remind_interval_seconds, priority, parent_id, note to change"
        )
    # meiva:note
    # A parent-only reparent (no business field changed, no note) is structural tree maintenance,
    # moving a task between parents without changing its work, so it must not notify, and
    # therefore must not resurrect, the owner. Batch reparenting used to fire one owner
    # notification per move, each auto-resurrecting a terminated owner (62-agent wake storm,
    # 2026-08-27).
    parent_only = parent_id is not UNSET_FIELD and nothing_to_update(sets, note)
    # meiva:note
    # The reminder window restarts only for a write that changes the work (see
    # `base.agents.tasks.reminder_window`); `write_task_update` decides that against the locked row
    # and appends the counter clauses there.
    write = UpdateWrite(
        status=status,
        title=title,
        description=description,
        results=results,
        owner=_update_value(owner),
        priority=priority,
        parent_id=_update_value(parent_id),
        parent_written=parent_id is not UNSET_FIELD,
        remind_interval_seconds=_update_value(remind_interval_seconds),
        note=note,
    )
    reset_on = _reminder_reset_on()

    from ._task_receipts import record_update, replay_update, update_identity

    actor, operation_key = update_identity(operation_key, context=context)
    request_body = update_request(
        {
            "status": status,
            "title": title,
            "description": description,
            "results": results,
            "owner": owner,
            "remind_interval_seconds": remind_interval_seconds,
            "priority": priority,
            "parent_id": parent_id,
            "note": note,
        },
        unchanged=UNSET_FIELD,
    )
    with context.sql.transaction(), context.sql.cursor() as cur:
        if replay_update(cur, actor, task_id, operation_key, request_body, context=context):
            return None
        if not isinstance(parent_id, Unset):
            # meiva:note
            # `None` means the system root, so the change set is measured against the resolved id:
            # a reparent to the parent a task already has is not a change.
            resolved_parent = resolve_reparent(cur, task_id, parent_id)
            sets.append("parent_id = %s")
            params.append(resolved_parent)
            write = replace(write, parent_id=resolved_parent)
        old_owner, current_title, new_owner, updated_event = write_task_update(
            cur,
            task_id,
            sets,
            params,
            payload,
            changes,
            write,
            reset_on,
            actor,
        )
        if parent_id is not UNSET_FIELD:
            changes.append("parent → root" if parent_id is None else f"parent → #{parent_id}")
        task_title = title if title is not None else current_title
        note_events = _queue_after_update(
            cur,
            task_id,
            task_title,
            old_owner,
            new_owner,
            actor,
            changes,
            owner_changed=owner_actually_changed(owner_changing, old_owner, new_owner),
            parent_only=parent_only,
        )
        record_update(cur, actor, task_id, operation_key, request_body)
    return _recorded_publication(
        actor,
        task_id,
        (updated_event, *note_events),
        closed_title=task_title if status == "done" else None,
    )


# meiva:note
# The emission half of an update: the recorded events, a live refresh of every open task board and,
# when the update closed the task, one log line. A replayed commit publishes nothing.
def publish_update(committed: TaskPublication | None, bus: EventBus) -> None:
    if committed is None:
        return
    # meiva:note
    # Deferred imports keep the psycopg stack off plugin autoload (task #3816).
    from base import telemetry
    from base.events.live import announce

    for event in publication_events(committed):
        telemetry.emit_prepared(event)
    announce.publish_task_updated_sync(bus, committed.actor, committed.task_id)
    if committed.closed_title is not None:
        _log_closed(committed.actor, committed.task_id, committed.closed_title)


# meiva:note
# A closed task is a log line, not a notification: nobody has to answer it, and
# the task board, the committed row and its `task_updated` event already record
# the close.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
def _log_closed(actor: int, task_id: int, title: str) -> None:
    logger.info(
        "task #{task_id} closed by agent {actor}: {title}",
        task_id=task_id,
        actor=actor,
        title=title,
    )


def _queue_after_update(
    cur: psycopg.Cursor,
    task_id: int,
    title: str,
    old_owner: int | None,
    new_owner: int | None,
    actor: int,
    changes: builtins.list[str],
    *,
    owner_changed: bool,
    parent_only: bool,
) -> builtins.list[Event]:
    """Enqueue policy-approved updates before the task transaction commits."""
    if owner_changed:
        from base.agents.tasks.delivery import supersede_task_assignments

        supersede_task_assignments(cur, task_id)
    if parent_only:
        return []
    if owner_changed:
        return _queue_owner_change(
            cur, task_id, title, old_owner, new_owner, actor, changes=changes
        )
    if new_owner is not None and actor != new_owner and not is_terminated(cur, new_owner):
        return _queue_owner_updated(cur, task_id, title, new_owner, actor, changes)
    return []


def _queue_owner_change(
    cur: psycopg.Cursor,
    task_id: int,
    title: str,
    old_owner: int | None,
    new_owner: int | None,
    actor: int,
    description: str | None = None,
    changes: builtins.list[str] | None = None,
) -> builtins.list[Event]:
    """Commit assignment directions with their task mutation, never HTTP-send."""
    from base.agents.tasks.delivery import enqueue_task_notifications

    notes = owner_change_notifications(
        task_id,
        title,
        old_owner,
        new_owner,
        actor=actor,
        previous_owner_terminated=old_owner is None or is_terminated(cur, old_owner),
        description=description,
        changes=changes,
    )
    receipts = enqueue_task_notifications(cur, task_id, notes, f"agent:{actor}")
    return [receipt.event for receipt in receipts if receipt.event is not None]


def _queue_owner_updated(
    cur: psycopg.Cursor,
    task_id: int,
    title: str,
    owner: int,
    actor: int,
    changes: builtins.list[str],
) -> builtins.list[Event]:
    """Queue an informational update while preserving the no-resurrection policy."""
    from base.agents.tasks.delivery import enqueue_task_notifications

    detail = "\n".join(f"- {change}" for change in changes)
    note = TaskOwnerNotification(
        owner,
        f'Task #{task_id} "{title}" was updated by agent #{actor}:\n{detail}',
        resurrect=False,
        link_task=True,
    )
    receipts = enqueue_task_notifications(cur, task_id, [note], f"agent:{actor}")
    return [receipt.event for receipt in receipts if receipt.event is not None]


def is_terminated(cur: psycopg.Cursor, agent_id: int) -> bool:
    """True when an agent is gone -- terminated, or absent from agents_meta.

    Gates only the previous-owner notification: a terminated former owner is
    left asleep rather than resurrected just to be told a task left it."""
    cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
    meta = cur.fetchone()
    return meta is None or meta[0] == "terminated"


def log(task_id: int, message: str, *, operation_key: str) -> None:
    """Append one timestamped line; reuse operation_key to replay without appending again."""
    task_id = coerce_typed(task_id, "task_id", int)
    message = coerce_str(message, "message")
    update(task_id, note=message, operation_key=operation_key)


def get(task_id: int) -> Task:
    """Return the task with this id."""
    task_id = coerce_typed(task_id, "task_id", int)
    with meiva.DB.cursor() as cur:
        cur.execute(f"SELECT {TASK_COLUMNS} FROM agent_tasks WHERE id = %s", (task_id,))  # noqa: S608
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"task {task_id} does not exist")
    return task_from_row(row)


def _where_clause(filters: builtins.list[str]) -> str:
    """' WHERE a AND b' for the given filters, or '' when there are none."""
    return (" WHERE " + " AND ".join(filters)) if filters else ""


def _build_list_query(
    parent: int | None,
    owner: int | None,
    status: str | None,
    recursive: bool,  # noqa: FBT001 — internal helper flag, always passed by name
) -> tuple[str, builtins.list[object]]:
    """SQL + params for list(): a plain filter query, or a recursive CTE over
    the whole subtree rooted at `parent`. parent_id always points at an older
    row (a child is created after its parent), so the tree cannot cycle and the
    recursion terminates. owner / status filter the subtree at the end."""
    filters: builtins.list[str] = []
    params: builtins.list[object] = []
    if owner is not None:
        filters.append("owner = %s")
        params.append(owner)
    if status is not None:
        filters.append("status = %s")
        params.append(status)

    if recursive and parent is not None:
        sql = (
            "WITH RECURSIVE subtree AS ("  # noqa: S608
            " SELECT * FROM agent_tasks WHERE parent_id = %s"
            " UNION ALL"
            " SELECT c.* FROM agent_tasks c JOIN subtree s ON c.parent_id = s.id"
            f") SELECT {TASK_COLUMNS} FROM subtree{_where_clause(filters)} ORDER BY created_at, id"
        )
        return sql, [parent, *params]

    if parent is not None:
        filters.append("parent_id = %s")
        params.append(parent)
    sql = f"SELECT {TASK_COLUMNS} FROM agent_tasks{_where_clause(filters)} ORDER BY created_at, id"  # noqa: S608
    return sql, params


def list(
    *,
    parent: int | None = None,
    owner: int | None = None,
    status: str | None = None,
    recursive: bool = False,
) -> builtins.list[Task]:
    """In creation order; no filters lists every task.

    Args:
        parent: keep only direct subtasks of this task; with recursive=True,
            its whole descendant subtree.
    """
    parent = coerce_typed(parent, "parent", int, allow_none=True)
    owner = coerce_typed(owner, "owner", int, allow_none=True)
    status = coerce_str(status, "status", allow_none=True)
    recursive = coerce_typed(recursive, "recursive", bool)
    if status is not None and status not in TASK_STATUS_VALUES:
        raise ValueError(f"status must be one of {sorted(TASK_STATUS_VALUES)}, got {status!r}")

    sql, params = _build_list_query(parent, owner, status, recursive)
    with meiva.DB.cursor() as cur:
        cur.execute(sql, params)
        return [task_from_row(r) for r in cur.fetchall()]
