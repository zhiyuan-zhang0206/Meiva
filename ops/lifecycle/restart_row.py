# meiva:note
# The lifecycle status and harness of an agent's `agents_meta` row, read before a restart decides
# whether it is a harness transition.

from __future__ import annotations

from dataclasses import dataclass

from psycopg_pool import ConnectionPool

from base.api_contracts.agent_contract import AgentNotFound
from base.db.rows import query_one

__all__ = ["read_restart_status_and_harness_blocking"]


# meiva:note
# The lifecycle status and harness columns of an `agents_meta` row.
@dataclass(frozen=True)
class _StatusAndHarness:
    status: str
    harness: str


# meiva:note
# The agent's status and harness, as plain strings; an absent agent is `AgentNotFound`.
def read_restart_status_and_harness_blocking(
    db_pool: ConnectionPool, agent_id: int
) -> tuple[str, str]:
    with db_pool.connection() as conn:
        row = query_one(
            conn,
            _StatusAndHarness,
            "SELECT status, harness FROM agents_meta WHERE id = %s",
            (agent_id,),
        )
    if row is None:
        raise AgentNotFound(f"agent {agent_id} does not exist")
    return row.status, row.harness
