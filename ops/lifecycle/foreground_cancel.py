from dataclasses import dataclass

from base.agents.inbox.insert import insert_inbound_message
from base.agents.messages.inbound import InboundKind
from base.api_contracts.agent_contract import AgentNotFound, AgentStatus, CancelResult, Harness
from base.db import Database
from base.db.rows import IdRow, query_one
from base.events.live.bus import EventBus


@dataclass(frozen=True)
class _Target:
    status: str
    harness: str


# meiva:note
# Serialize the running-state check and enqueue with agent admission and settlement.
# Repeated clicks share the pending request. Claim consumes it before starting new work.
def enqueue_external_cancel(db: Database, bus: EventBus, agent_id: int) -> CancelResult | None:
    with db.write_transaction() as conn:
        target = query_one(
            conn,
            _Target,
            "SELECT status, harness FROM agents_meta WHERE id = %s FOR UPDATE",
            (agent_id,),
        )
        if target is None:
            raise AgentNotFound(f"agent {agent_id} does not exist")
        if Harness(target.harness) is Harness.MEIVA:
            return None
        if AgentStatus(target.status) is not AgentStatus.RUNNING:
            return CancelResult.NOT_RUNNING
        pending = query_one(
            conn,
            IdRow,
            "SELECT id FROM inbound_messages WHERE agent_id = %s "
            "AND kind = 'cancel' AND status = 'pending' ORDER BY id LIMIT 1",
            (agent_id,),
        )
        if pending is None:
            insert_inbound_message(
                conn, agent_id, "", "user", kind=InboundKind.CANCEL, database=db, bus=bus
            )
        return CancelResult.ENQUEUED
