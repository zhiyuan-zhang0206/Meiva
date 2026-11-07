"""Accept task escalation from locked current state, before any live hints."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import psycopg
from psycopg_pool import ConnectionPool

from base.agents.inbox.insert import insert_inbound_message_in_transaction
from base.agents.tasks.status import TaskStatus
from base.db.rows import query_all
from base.db.transaction import write_transaction
from meiva_builtins.plugins.meiva_fleet.task_maintenance.routing import (
    nearest_manager,
    stage_human_notice,
)


@dataclass(frozen=True)
class EscalationReceipt:
    recipient: int
    task_ids: list[int]
    leg: Literal["user", "delegator"]
    inbound_id: int | None
    content: str
    # meiva:note
    # The staged `notification_events` row of the user leg, None when the spine
    # suppressed it (the category is switched off, or the dedupe window is open)
    # and for a delegator digest, which is an inbound note rather than an inbox
    # notice. The daemon announces it after this transaction commits; a lost
    # announce leaves the row unannounced and the outbox sweep retries it.
    # meiva:link base/events/notifications/docs/notifications.meiva.okf.md
    notification_id: int | None = None


@dataclass(frozen=True)
class _Task:
    id: int
    parent: int | None
    owner: int | None
    title: str
    status: TaskStatus
    # meiva:note
    # Escalation counts cadence periods of silence, not delivered reminders: the delivery
    # window can hold a reminder back, and a task must not look younger than it is because
    # nobody spoke. `elapsed_seconds` and `remind_interval_seconds` are read from the locked
    # row so the eligibility recheck sees the values the lock protects.
    elapsed_seconds: int
    remind_interval_seconds: int | None
    reminder_count: int
    escalated_at: datetime | None
    priority: str
    is_root: bool


# meiva:note
# A task and its parent, as the candidate query selects them.
@dataclass(frozen=True)
class _Edge:
    id: int
    parent_id: int | None


# meiva:note
# A locked task row, named as the table names its columns; `status` is converted to `TaskStatus`
# when the row becomes a `_Task`.
@dataclass(frozen=True)
class _TaskRow:
    id: int
    parent_id: int | None
    owner: int | None
    title: str
    status: str
    elapsed_seconds: int
    remind_interval_seconds: int | None
    reminder_count: int
    escalated_at: datetime | None
    priority: str
    is_root: bool


def _lock_candidates(
    cur: psycopg.Cursor, threshold: int, backoff_seconds: float
) -> tuple[list[int], dict[int, _Task]]:
    # meiva:note
    # Discovery never authorizes an effect. Lock the union in stable id order;
    # SKIP LOCKED avoids reversing a writer's child -> parent lock order.
    # NO KEY UPDATE still fences mutations while allowing a gateway notice
    # creator holding the agent identity lock to reference task_id by FK.
    # A task is a candidate once it has been silent for `threshold` of its own cadence
    # periods — the same `GREATEST(backoff, remind_interval_seconds)` the reminder pass
    # schedules by — and at least one reminder actually left, so an escalation always
    # answers something the owner received. Counting reminders alone would let the
    # delivery window stretch this: the window thins the reminders an owner gets, so
    # three of them then span far more than three cadence periods (2026-11-07 ruling,
    # task #6354 — escalation must not be keyed on a delivery policy).
    candidates = query_all(
        cur.connection,
        _Edge,
        "SELECT id,parent_id FROM agent_tasks WHERE status='in_progress' "
        "AND NOT is_root AND reminder_count >= 1 "
        "AND (reminders_paused_until IS NULL OR reminders_paused_until <= now()) "
        "AND now() - updated_at >= make_interval(secs => %s::float8 * "
        "GREATEST(%s, COALESCE(remind_interval_seconds, 0))) ORDER BY id",
        (float(threshold), float(backoff_seconds)),
    )
    ids = sorted({i for edge in candidates for i in (edge.id, edge.parent_id) if i is not None})
    locked = {
        row.id: _Task(
            id=row.id,
            parent=row.parent_id,
            owner=row.owner,
            title=row.title,
            status=TaskStatus(row.status),
            elapsed_seconds=row.elapsed_seconds,
            remind_interval_seconds=row.remind_interval_seconds,
            reminder_count=row.reminder_count,
            escalated_at=row.escalated_at,
            priority=row.priority,
            is_root=row.is_root,
        )
        for row in query_all(
            cur.connection,
            _TaskRow,
            "SELECT id,parent_id,owner,title,status,"
            "EXTRACT(EPOCH FROM (now() - updated_at))::bigint AS elapsed_seconds,"
            "remind_interval_seconds,reminder_count,escalated_at,priority,is_root "
            "FROM agent_tasks WHERE id=ANY(%s) ORDER BY id FOR NO KEY UPDATE SKIP LOCKED",
            (ids,),
        )
    }
    return [edge.id for edge in candidates], locked


# meiva:note
# Escalation copy states the evidence, never a verdict on the owner. This pass reads
# task bookkeeping only — status, reminder_count, escalated_at and the ancestor
# owners — and a note on the task restarts the reminder window, so N reminders in a
# row mean nothing was recorded on the task since they began: "no state change" is
# the strongest claim the daemon can support. An owner that answered in chat, worked
# in a shell or advanced a peer's task is indistinguishable here from one that is
# gone, and the same words have to serve both (2026-10-30 ruling, task #5862).
# Liveness lives in `agents_meta` and `routing.nearest_manager` already reads it to
# pick a recipient; it is deliberately not a claim about the owner.
# meiva:link meiva_builtins/plugins/meiva_fleet/task_maintenance/docs/task-maintenance/escalation.meiva.okf.md
_STATE_CHANGE_BASIS = "a note or a change on the task would have restarted the reminders"


# meiva:note
# The evidence an escalation states is how long the task has been silent, read from the
# locked row. It replaced the reminder count on 2026-11-07 (task #6354): reminders are
# throttled per owner by the delivery window, so a count no longer measures time, and an
# escalation that quoted it would understate how long nobody has touched the task.
def _silent_for(task: _Task) -> str:
    return f"{task.elapsed_seconds / 3600:.1f}h"


def _user_notice(
    cur: psycopg.Cursor,
    conn: psycopg.Connection,
    task: _Task,
    *,
    notice_ttl_limit_seconds: float,
) -> EscalationReceipt | None:
    if task.owner is None:
        raise ValueError("human escalation requires a task owner")
    title = (
        f'Task #{task.id} "{task.title}" — no state change for {_silent_for(task)}; '
        "reassign or cancel"
    )
    content = (
        f"Agent #{task.owner} has not changed this task's state for {_silent_for(task)} "
        f"({_STATE_CHANGE_BASIS}), and no live agent owns any ancestor task to catch it. "
        "Reassign it to another agent, cancel it, or reply to remind the owner once more."
    )
    staged = stage_human_notice(
        cur,
        conn,
        agent_id=task.owner,
        task_id=task.id,
        title=title,
        content=content,
        priority=task.priority,
        notice_ttl_limit_seconds=notice_ttl_limit_seconds,
    )
    if staged is None:
        return None
    return EscalationReceipt(task.owner, [task.id], "user", None, content, staged[1])


def _delegator_note(cur: psycopg.Cursor, owner: int, tasks: list[_Task]) -> EscalationReceipt:
    lines = [f"Subtasks still open with no state change ({_STATE_CHANGE_BASIS}):"]
    lines.extend(
        f'- #{task.id} "{task.title}" — owner #{task.owner}, '
        f"no state change for {_silent_for(task)}"
        for task in tasks
    )
    content = "\n".join(lines)
    inbound_id, _ = insert_inbound_message_in_transaction(
        cur,
        owner,
        content,
        "system",
        kind="system_note",
        payload={"note_tag": "task"},
    )
    task_ids = [task.id for task in tasks]
    cur.execute("UPDATE agent_tasks SET escalated_at=now() WHERE id=ANY(%s)", (task_ids,))
    return EscalationReceipt(owner, task_ids, "delegator", inbound_id, content)


# meiva:note
# The delegator leg addresses the task's manager, not its direct parent owner:
# an agent terminated after delegating cannot catch the stall, so the walk
# continues to the nearest ancestor owner that is still alive. No such owner
# leaves the human queue as the only recipient. The parent row still has to be
# locked (a concurrent writer's reparent defers this task) — the manager lookup
# reads the chain behind it.
def _eligible_groups(
    candidates: list[int],
    locked: dict[int, _Task],
    threshold: int,
    backoff_seconds: float,
    managers: dict[int, int | None],
) -> tuple[dict[int, list[_Task]], list[_Task]]:
    delegated: dict[int, list[_Task]] = defaultdict(list)
    human: list[_Task] = []
    for task_id in candidates:
        task = locked.get(task_id)
        if (
            task is None
            or task.is_root
            or task.status is not TaskStatus.IN_PROGRESS
            or task.reminder_count < 1
            or task.elapsed_seconds
            < threshold * max(backoff_seconds, task.remind_interval_seconds or 0)
        ):
            continue
        parent = locked.get(task.parent) if task.parent is not None else None
        if parent is None or task.owner is None:
            continue
        manager = managers.get(task_id)
        if manager is None:
            human.append(task)
        elif task.escalated_at is None:
            delegated[manager].append(task)
    return delegated, human


# meiva:note
# `threshold` is counted in the task's own cadence periods
# (`GREATEST(backoff_seconds, remind_interval_seconds)`), not in delivered reminders:
# delivery is throttled per owner by the digest window, and a task must not read
# younger than it is because the owner was not spoken to (2026-11-07, task #6354).
def accept_escalations(
    pool: ConnectionPool,
    threshold: int,
    backoff_seconds: float,
    *,
    notice_ttl_limit_seconds: float,
) -> list[EscalationReceipt]:
    """Commit each eligible digest/notice from current task and parent rows.

    Parent changes discovered after locking are deferred if the actual parent
    is not locked. Agent identity locks serialize notice creation with gateway
    create's open check and local-id allocation. All live hints happen later.
    """
    with write_transaction(pool) as conn, conn.cursor() as cur:
        candidates, locked = _lock_candidates(cur, threshold, backoff_seconds)
        managers = nearest_manager(cur, [task_id for task_id in candidates if task_id in locked])
        delegated, human = _eligible_groups(
            candidates, locked, threshold, backoff_seconds, managers
        )
        # Same physical identity lock as gateway notice create. Ordered locks
        # prevent two overlapping human batches from reversing agent order.
        owners = sorted(set(delegated) | {task.owner for task in human if task.owner is not None})
        cur.execute("SELECT id FROM agents WHERE id=ANY(%s) ORDER BY id FOR UPDATE", (owners,))
        receipts = [
            _delegator_note(cur, owner, tasks) for owner, tasks in sorted(delegated.items())
        ]
        for task in human:
            receipt = _user_notice(
                cur, conn, task, notice_ttl_limit_seconds=notice_ttl_limit_seconds
            )
            if receipt is not None:
                receipts.append(receipt)
        return receipts
