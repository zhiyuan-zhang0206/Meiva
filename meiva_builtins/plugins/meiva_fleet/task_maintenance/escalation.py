"""Accept task escalation from locked current state, before any live hints."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import psycopg
from psycopg_pool import ConnectionPool

from base.agents.inbox.insert import insert_inbound_message_in_transaction
from base.agents.tasks.status import TaskStatus
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
    reminder_count: int
    escalated_at: datetime | None
    priority: str
    is_root: bool


def _lock_candidates(cur: psycopg.Cursor, threshold: int) -> tuple[list[int], dict[int, _Task]]:
    # Discovery never authorizes an effect. Lock the union in stable id order;
    # SKIP LOCKED avoids reversing a writer's child -> parent lock order.
    # NO KEY UPDATE still fences mutations while allowing a gateway notice
    # creator holding the agent identity lock to reference task_id by FK.
    cur.execute(
        "SELECT id,parent_id FROM agent_tasks WHERE status='in_progress' "
        "AND reminder_count >= %s AND NOT is_root "
        "AND (reminders_paused_until IS NULL OR reminders_paused_until <= now()) ORDER BY id",
        (threshold,),
    )
    candidates = cur.fetchall()
    ids = sorted({i for task, parent in candidates for i in (task, parent) if i is not None})
    cur.execute(
        "SELECT id,parent_id,owner,title,status,reminder_count,escalated_at,priority,is_root "
        "FROM agent_tasks WHERE id=ANY(%s) ORDER BY id FOR NO KEY UPDATE SKIP LOCKED",
        (ids,),
    )
    locked = {
        row[0]: _Task(
            row[0], row[1], row[2], row[3], TaskStatus(row[4]), row[5], row[6], row[7], row[8]
        )
        for row in cur.fetchall()
    }
    return [row[0] for row in candidates], locked


# meiva:note
# Escalation copy states the evidence, never a verdict on the owner. This pass reads
# task bookkeeping only — status, reminder_count, escalated_at and the ancestor
# owners — and `tasks.log` deliberately leaves `updated_at` alone, so "no state
# change" is the strongest claim the daemon can support. An owner that answered in
# chat, worked in a shell or advanced a peer's task is indistinguishable here from
# one that is gone, and the same words have to serve both (2026-10-30 ruling, task
# #5862). Liveness lives in `agents_meta` and `routing.nearest_manager` already
# reads it to pick a recipient; it is deliberately not a claim about the owner.
# meiva:link meiva_builtins/plugins/meiva_fleet/task_maintenance/docs/task-maintenance/escalation.meiva.okf.md
_STATE_CHANGE_BASIS = "a tasks.log note is not a state change"


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
        f'Task #{task.id} "{task.title}" — {task.reminder_count} reminders with no state '
        "change; reassign or cancel"
    )
    content = (
        f"Agent #{task.owner} has not changed this task's state after "
        f"{task.reminder_count} reminders ({_STATE_CHANGE_BASIS}), and no live agent owns "
        "any ancestor task to catch it. Reassign it to another agent, cancel it, or reply "
        "to remind the owner once more."
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
    lines = [
        "Subtasks still open after repeated reminders — task state unchanged "
        f"({_STATE_CHANGE_BASIS}):"
    ]
    lines.extend(
        f'- #{task.id} "{task.title}" — owner #{task.owner}, {task.reminder_count} reminders'
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
            or task.reminder_count < threshold
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


def accept_escalations(
    pool: ConnectionPool, threshold: int, *, notice_ttl_limit_seconds: float
) -> list[EscalationReceipt]:
    """Commit each eligible digest/notice from current task and parent rows.

    Parent changes discovered after locking are deferred if the actual parent
    is not locked. Agent identity locks serialize notice creation with gateway
    create's open check and local-id allocation. All live hints happen later.
    """
    with write_transaction(pool) as conn, conn.cursor() as cur:
        candidates, locked = _lock_candidates(cur, threshold)
        managers = nearest_manager(cur, [task_id for task_id in candidates if task_id in locked])
        delegated, human = _eligible_groups(candidates, locked, threshold, managers)
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
