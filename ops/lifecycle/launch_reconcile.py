"""Repeatable wake reconciliation for a transactionally accepted launch retry."""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from psycopg_pool import ConnectionPool

from base.agents.inbox.wake import publish_inbound_wake
from base.api_contracts.launch_retry import LaunchReconciled, LaunchReconcileRequest
from base.db import Database
from base.db.rows import query_one
from base.db.transaction import write_transaction
from base.events.live.bus import EventBus


# meiva:note
# The receipt that names the agent and machine a launch attempt was accepted for.
@dataclass(frozen=True)
class _Receipt:
    agent_id: int
    machine: str


# meiva:note
# The `agents_meta` columns that decide whether a retry attempt is still waiting to be admitted.
@dataclass(frozen=True)
class _Admission:
    machine: str
    last_launch_attempt_id: UUID | None
    status: str
    last_admission_at: datetime | None


async def reconcile_launch_op(
    db: Database,
    bus: EventBus,
    body: LaunchReconcileRequest,
    pool: ConnectionPool,
    *,
    local_machine: str,
) -> LaunchReconciled:
    """Repeat a wake hint without inserting a prompt, rotating or forcing a host."""
    return await asyncio.to_thread(reconcile, db, bus, body, pool, local_machine=local_machine)


def reconcile(
    db: Database,
    bus: EventBus,
    body: LaunchReconcileRequest,
    pool: ConnectionPool,
    *,
    local_machine: str,
) -> LaunchReconciled:
    with write_transaction(pool) as conn:
        receipt = query_one(
            conn,
            _Receipt,
            "SELECT agent_id, machine FROM agent_launch_retry_receipts WHERE launch_attempt_id=%s",
            (body.launch_attempt_id,),
        )
        if receipt is None or receipt.machine != local_machine:
            raise ValueError("launch retry receipt is missing or belongs to another machine")
        agent_id = receipt.agent_id
        row = query_one(
            conn,
            _Admission,
            "SELECT machine, last_launch_attempt_id, status, last_admission_at "
            "FROM agents_meta WHERE id=%s FOR UPDATE",
            (agent_id,),
        )
        if row != _Admission(receipt.machine, body.launch_attempt_id, "idling", None):
            return LaunchReconciled(wake_published=False)
        # Hold the metadata fence through publication. Concurrent admission,
        # termination, placement and later retry cannot pass between check and
        # wake. A hint already published can be consumed later: the native host
        # still owns admission and claims only existing durable inbound work.
        published = publish_inbound_wake(db, bus, agent_id, "0")
        return LaunchReconciled(wake_published=published)
