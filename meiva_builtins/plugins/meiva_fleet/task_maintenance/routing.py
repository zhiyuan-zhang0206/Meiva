# meiva:note
# Who can still act on a due task notification. A reminder or an escalation is worth
# sending only if its recipient can act: the reminder pass addresses the task owner and
# the escalation pass addresses a delegator, but either may already be terminated. Both
# passes therefore ask this module the same question — who manages this task now — so
# neither can route to an agent that is already gone.
# meiva:link meiva_builtins/plugins/meiva_fleet/task_maintenance/docs/task-maintenance/task-maintenance.md
from collections.abc import Iterable
from dataclasses import dataclass

import psycopg

from base.api_contracts.agent_contract import AgentStatus
from base.db.rows import query_all


# meiva:note
# `nearest_manager` answers "who manages this task now": the owner of the nearest
# ancestor task that is not terminated, so the direct parent (the delegator) answers the
# common case while a chain running through the dead owner's own container tasks, or
# through another dead agent, keeps walking up. None means every ancestor is ownerless
# or dead and no agent can act — the caller records that and sends nothing.
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
