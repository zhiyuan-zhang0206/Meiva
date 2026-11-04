# meiva:note
# Who can still act on a due task notification. A reminder or an escalation is worth
# sending only if its recipient can act: the reminder pass addresses the task owner and
# the escalation pass addresses a delegator, but either may already be terminated. Both
# passes therefore ask this module the same question — who manages this task now — so
# neither can route to an agent that is already gone.
# meiva:link meiva_builtins/plugins/meiva_fleet/task_maintenance/docs/task-maintenance/task-maintenance.meiva.okf.md
from collections.abc import Iterable
from dataclasses import dataclass

import psycopg

from base.api_contracts.agent_contract import AgentStatus
from base.db.rows import IdRow, query_all, query_one
from base.events.notifications.notices import stage_notice_notification


# meiva:note
# `nearest_manager` answers "who manages this task now": the owner of the nearest
# ancestor task that is not terminated, so the direct parent (the delegator) answers the
# common case while a chain running through the dead owner's own container tasks, or
# through another dead agent, keeps walking up. None means every ancestor is ownerless
# or dead and no agent can act — the caller falls back to the human queue.
#
# The walk reads one level per query and continues only past an ancestor that cannot
# take the message: no owner (the ownerless system root) or a terminated owner. A
# repeated node ends that task's walk, so a corrupt parent cycle terminates instead of
# looping. A recursive CTE would carry the same two stop conditions in SQL and could not
# be read against the message copy. Reads only: the caller owns the transaction, and an
# ancestor's owner may change under it.
# meiva:note
# A task and its parent, as the first lookup of the walk selects them.
@dataclass(frozen=True)
class _Edge:
    id: int
    parent_id: int | None


# meiva:note
# An ancestor task with its owner's liveness, named as the walk's statement selects them. `owner`
# is null on the ownerless system root, and `status` is null when the owner has no `agents_meta` row.
@dataclass(frozen=True)
class _Ancestor:
    id: int
    parent_id: int | None
    owner: int | None
    status: str | None


def nearest_manager(cur: psycopg.Cursor, task_ids: Iterable[int]) -> dict[int, int | None]:
    ids = list(task_ids)
    resolved: dict[int, int | None] = dict.fromkeys(ids)
    if not ids:
        return resolved
    pending = {
        edge.id: edge.parent_id
        for edge in query_all(
            cur.connection,
            _Edge,
            "SELECT id, parent_id FROM agent_tasks WHERE id = ANY(%s)",
            (ids,),
        )
        if edge.parent_id is not None
    }
    visited: dict[int, set[int]] = {task_id: set() for task_id in ids}
    while pending:
        nodes = {
            row.id: row
            for row in query_all(
                cur.connection,
                _Ancestor,
                "SELECT t.id, t.parent_id, t.owner, m.status FROM agent_tasks t "
                "LEFT JOIN agents_meta m ON m.id = t.owner WHERE t.id = ANY(%s)",
                (sorted(set(pending.values())),),
            )
        }
        next_pending: dict[int, int] = {}
        for task_id, node in pending.items():
            ancestor = nodes.get(node)
            if ancestor is None:
                visited[task_id].add(node)
                continue
            if ancestor.owner is not None and ancestor.status != AgentStatus.TERMINATED:
                resolved[task_id] = ancestor.owner
                continue
            visited[task_id].add(node)
            if ancestor.parent_id is not None and ancestor.parent_id not in visited[task_id]:
                next_pending[task_id] = ancestor.parent_id
        pending = next_pending
    return resolved


# meiva:note
# The human leg both passes fall back to: one require_response notice on the owner,
# which the human queue already surfaces and groups by agent. An open notice on the
# same agent suppresses a second one — the queue shows one thing at a time for an
# agent, and a second row would only repeat the first. `notification_id` is None when
# the spine suppressed the delivery (category switched off, dedupe window open); the
# notice row itself still exists for the queue. The caller owns the transaction and
# announces the staged notification after commit.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
def stage_human_notice(
    cur: psycopg.Cursor,
    conn: psycopg.Connection,
    *,
    agent_id: int,
    task_id: int | None,
    title: str,
    content: str,
    priority: str,
    notice_ttl_limit_seconds: float,
) -> tuple[int, int | None] | None:
    cur.execute(
        "SELECT 1 FROM agent_notices WHERE agent_id=%s AND resolved_at IS NULL LIMIT 1",
        (agent_id,),
    )
    if cur.fetchone() is not None:
        return None
    inserted = query_one(
        conn,
        IdRow,
        "INSERT INTO agent_notices "
        "(agent_id,local_id,task_id,title,content,priority,require_response,blocking,expire_at,"
        "source_verified_by) "
        "VALUES (%s,COALESCE((SELECT MAX(local_id) FROM agent_notices WHERE agent_id=%s),-1)+1, "
        "%s,%s,%s,%s,TRUE,FALSE,now()+make_interval(secs=>%s),'internal') RETURNING id",
        (
            agent_id,
            agent_id,
            task_id,
            title,
            content,
            priority,
            notice_ttl_limit_seconds,
        ),
    )
    if inserted is None:
        raise RuntimeError("human notice insert returned no row")
    notice_id = inserted.id
    return notice_id, stage_notice_notification(conn, agent_id=agent_id, notice_id=notice_id)
