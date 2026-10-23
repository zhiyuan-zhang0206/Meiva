# meiva:note
# Who dispatched a launch, as a function of what the launcher could read. The launcher reads the dispatching agent, its ancestors and its directory row from the cluster; a read that failed arrives as None and degrades the block to less lineage instead of failing the launch. A missing label is not an error either: the directory may not know the agent yet, and the block still names it by id. Only the id is load-bearing.
# meiva:link meiva/shell/coding_tools/docs/coding-tools.meiva.okf.md
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from base.sessions.provenance import DispatchProvenance

_CHAIN_LENGTH = 5


# meiva:note
# The launching process's lineage facts: its agent id (None without an agent identity), the ids of the agent's ancestors in the order the cluster returned them (None when the lookup failed) and the (id, label) rows of the directory page that matched the agent (None when the lookup failed).
@dataclass(frozen=True)
class DispatchFacts:
    parent_agent_id: int | None
    ancestor_ids: tuple[int, ...] | None
    directory: tuple[tuple[int, str | None], ...] | None


# meiva:note
# A missing --why is a usage error: the reason is the one field the launcher cannot derive, and a worker without it cannot judge whether the work fits. Task metadata needs a task. Returns the usage message, None when the flags are consistent.
def lineage_error(
    why: str, for_task: int | None, task_label: str | None, priority: str | None
) -> str | None:
    if not why.strip():
        return "--why is required: one line saying why this work is delegated"
    if for_task is None and (task_label is not None or priority is not None):
        return "--task-label/--priority require --for-task"
    return None


# meiva:note
# The block for one launch. The spawn chain runs from the root ancestor to the dispatching agent, bounded to the last five.
def dispatch_provenance(
    facts: DispatchFacts,
    *,
    for_task: int | None,
    why: str,
    task_label: str | None,
    priority: str | None,
    now: dt.datetime,
) -> DispatchProvenance:
    parent_id = facts.parent_agent_id
    chain: tuple[str, ...] = ()
    label: str | None = None
    if parent_id is not None:
        if facts.ancestor_ids is not None:
            ids = [*(str(agent_id) for agent_id in reversed(facts.ancestor_ids)), str(parent_id)]
            chain = tuple(ids[-_CHAIN_LENGTH:])
        for agent_id, row_label in facts.directory or ():
            if agent_id == parent_id:
                label = row_label
                break
    return DispatchProvenance(
        parent_agent_id=parent_id,
        parent_label=label,
        spawn_chain=chain,
        dispatched_at=now,
        why=why,
        task_id=for_task,
        task_label=task_label,
        priority=priority,
    )
