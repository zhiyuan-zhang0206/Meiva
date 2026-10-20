# meiva:note
# Live-agent predicate and the live-agent listing.


from datetime import UTC, datetime

from base.api_contracts.agent_contract import AgentStatus
from base.db import Database

# A "live" agent is one currently holding a process (status running/idling) —
# the set a cluster-wide stop-the-world (quiesce before a schema migration) must
# drain. The three helpers below all key off this same predicate; the statuses
# live in ONE constant, passed as a parameter (`status = ANY(%s)`), so a "live"
# semantics change touches one line, not three literal copies (audit
# 05-gateway-lifecycle A3).
# R1 (Task #1021): the single "alive" predicate — status in {running, idling}
# AND lease unexpired. The lease (`agents_meta.lease_expires_at`, written at
# claim by `agent._starting.claim_agent_row`, renewed by the agent's loop)
# is the liveness authority: a process that died without writing 'terminated'
# leaves its status behind and the lease expires; a process that cannot renew
# (wedged, pre-lease code) is a zombie the reaper collects.
#
# The statuses live in ONE constant and the lease condition in ONE fragment
# (`ALIVE_SQL`), so a "live" semantics change touches one line, not the literal
# copies (audit 05-gateway-lifecycle A3 + r1-state-liveness).
ALIVE_STATUSES: tuple[str, ...] = (
    AgentStatus.RUNNING.value,
    AgentStatus.IDLING.value,
)

# The SQL half of the predicate. Every reader interpolates `status = ANY(%s)
# AND lease_expires_at > now()` (first parameter = list(ALIVE_STATUSES)) so
# "alive" stays one definition across queries of every shape.
ALIVE_SQL = "status = ANY(%s) AND lease_expires_at > now()"


def agent_is_alive(status: str | None, lease_expires_at: datetime | None) -> bool:
    """The Python half of the single alive predicate, for row-based checks.

    A row is alive iff its status says a process should own it AND its lease is
    unexpired. `None` lease (never granted — pre-lease code) reads as dead,
    matching the SQL fragment: the reaper collects such rows so they land on
    code that renews.
    """
    if status not in ALIVE_STATUSES:
        return False
    if lease_expires_at is None:
        return False
    return lease_expires_at > datetime.now(UTC)


def list_live_agent_ids(db: Database, machine: str | None = None) -> list[int]:
    """IDs of agents currently holding a process (status running/idling).

    `machine` restricts to one host's agents (the per-host quiesce); None
    returns the whole cluster (the rollout's stop-the-world).
    """
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM agents_meta "  # noqa: S608 — ALIVE_SQL is a module constant
            f"WHERE {ALIVE_SQL} AND (%s::text IS NULL OR machine = %s)",
            (list(ALIVE_STATUSES), machine, machine),
        )
        return [row[0] for row in cur.fetchall()]
