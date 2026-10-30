# meiva:note
# Who can still act on a due task notification. A reminder or an escalation is worth
# sending only if its recipient can act: the reminder pass addresses the task owner and
# the escalation pass addresses a delegator, but either may already be terminated. Both
# passes therefore ask this module the same question — who manages this task now — so
# neither can route to an agent that is already gone.
# meiva:link meiva_builtins/plugins/meiva_fleet/task_maintenance/docs/task-maintenance/task-maintenance.meiva.okf.md
from collections.abc import Iterable

import psycopg

from base.api_contracts.agent_contract import AgentStatus
from base.events.notifications.notices import stage_notice_notification
from base.packages.plugins.flags import read_declared_flag
from meiva_builtins.plugins.meiva_fleet.default_config import contribute


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
def nearest_manager(cur: psycopg.Cursor, task_ids: Iterable[int]) -> dict[int, int | None]:
    ids = list(task_ids)
    resolved: dict[int, int | None] = dict.fromkeys(ids)
    if not ids:
        return resolved
    cur.execute("SELECT id, parent_id FROM agent_tasks WHERE id = ANY(%s)", (ids,))
    pending = {task_id: int(parent) for task_id, parent in cur.fetchall() if parent is not None}
    visited: dict[int, set[int]] = {task_id: set() for task_id in ids}
    while pending:
        cur.execute(
            "SELECT t.id, t.parent_id, t.owner, m.status FROM agent_tasks t "
            "LEFT JOIN agents_meta m ON m.id = t.owner WHERE t.id = ANY(%s)",
            (sorted(set(pending.values())),),
        )
        nodes = {int(row[0]): row[1:] for row in cur.fetchall()}
        next_pending: dict[int, int] = {}
        for task_id, node in pending.items():
            parent_id, owner, status = nodes.get(node, (None, None, None))
            if owner is not None and status != AgentStatus.TERMINATED:
                resolved[task_id] = int(owner)
                continue
            visited[task_id].add(node)
            if parent_id is not None and int(parent_id) not in visited[task_id]:
                next_pending[task_id] = int(parent_id)
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
) -> tuple[int, int | None] | None:
    cur.execute(
        "SELECT 1 FROM agent_notices WHERE agent_id=%s AND resolved_at IS NULL LIMIT 1",
        (agent_id,),
    )
    if cur.fetchone() is not None:
        return None
    cur.execute(
        "INSERT INTO agent_notices "
        "(agent_id,local_id,task_id,title,content,priority,require_response,blocking,expire_at) "
        "VALUES (%s,COALESCE((SELECT MAX(local_id) FROM agent_notices WHERE agent_id=%s),-1)+1, "
        "%s,%s,%s,%s,TRUE,FALSE,now()+make_interval(secs=>%s)) RETURNING id",
        (
            agent_id,
            agent_id,
            task_id,
            title,
            content,
            priority,
            read_declared_flag("daemon.notice_ttl_limit_seconds", contribute().flags),
        ),
    )
    row = cur.fetchone()
    if row is None:
        raise RuntimeError("human notice insert returned no row")
    notice_id = int(row[0])
    return notice_id, stage_notice_notification(conn, agent_id=agent_id, notice_id=notice_id)
