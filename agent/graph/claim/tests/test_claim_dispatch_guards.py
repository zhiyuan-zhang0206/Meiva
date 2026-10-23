"""The claim node refuses an unknown inbound kind, and a pending newer inbound vetoes a terminate."""

import psycopg
import pytest
from langchain_core.messages import SystemMessage
from langgraph.graph import END
from psycopg_pool import AsyncConnectionPool

from agent.db import ClaimedInbound
from agent.graph.claim.node import settle_claimed_batch
from agent.graph.claim.tests.claim_support import (
    await_inbound_visible,
    insert_inbound_kind,
    make_runtime,
)
from agent.state import AgentState
from base.agents.inbox.insert import insert_inbound_message
from base.agents.tests.units import spawn_agent
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog


# meiva:note
# Unrecognized inbound kind = framework / DB schema desync: raise at once, never silently default
# to chat processing. The DB CHECK constraint keeps an unknown kind out of production rows, so the
# test hands the dispatch a claimed batch by value to reach its `case _:` fallback.
async def test_claim_unknown_kind_raises(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
):
    tid = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    runtime = make_runtime(ops_pool=aops_pool, database_gate=database_gate)
    batch = [ClaimedInbound(id=99, agent_id=tid, content="x", kind="bogus", source="system")]

    with pytest.raises(ValueError, match="Unknown inbound kind"):
        await settle_claimed_batch(runtime.context, AgentState(), tid, batch)


# meiva:note
# The other half of the race: the batch (terminate alone) is claimed, but a message lands in the
# queue before the exit is committed. The final recheck must veto the death and re-enter claim so
# the fresh message is dispatched: the terminate row is already consumed ('done'), no marker, no
# END, and the chat stays pending for the re-entered claim to pick up. The claim's grab marks the
# lifecycle row 'done' atomically, which the test does for the row it hands over.
async def test_claim_terminate_vetoed_by_pending_inbound_after_claim(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
):
    tid = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    terminate_id = insert_inbound_kind(db_conn, tid, "", "terminate", source="self")
    db_conn.execute("UPDATE inbound_messages SET status = 'done' WHERE id = %s", (terminate_id,))
    db_conn.commit()
    chat_id = insert_inbound_message(
        db_conn, tid, "message after the claim", source="user", bus=event_bus, database=database
    )
    await await_inbound_visible(aops_pool, chat_id)
    runtime = make_runtime(ops_pool=aops_pool, database_gate=database_gate)
    claimed = [
        ClaimedInbound(id=terminate_id, agent_id=tid, content="", kind="terminate", source="self")
    ]

    cmd = await settle_claimed_batch(
        runtime.context, AgentState(messages=[SystemMessage(content="sys")]), tid, claimed
    )

    # re-enter claim to dispatch the fresh message — not END, not a wake
    assert cmd.goto == "claim"
    assert cmd.goto != END
    # no terminate marker was committed
    assert cmd.update["messages"] == []  # type: ignore[index]
    # the newer chat is still pending for the re-entered claim to pick up
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM inbound_messages WHERE agent_id = %s AND kind = 'chat'", (tid,)
        )
        chat_row = cur.fetchone()
        assert chat_row is not None
        assert chat_row[0] == "pending"
    # the vetoed terminate row is consumed, never retried
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM inbound_messages WHERE agent_id = %s AND kind = 'terminate'", (tid,)
        )
        term_row = cur.fetchone()
        assert term_row is not None
        assert term_row[0] == "done"


# meiva:note
# Veto half 1: a same-batch chat newer than the terminate keeps the agent alive.
async def test_claim_same_batch_newer_chat_vetoes_the_terminate(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
):
    tid = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    terminate_id = insert_inbound_kind(db_conn, tid, "", "terminate", source="user")
    chat_id = insert_inbound_message(
        db_conn, tid, "message in the batch", source="user", bus=event_bus, database=database
    )
    await await_inbound_visible(aops_pool, chat_id)
    runtime = make_runtime(ops_pool=aops_pool, database_gate=database_gate)
    claimed = [
        ClaimedInbound(id=terminate_id, agent_id=tid, content="", kind="terminate", source="user"),
        ClaimedInbound(
            id=chat_id, agent_id=tid, content="message in the batch", kind="chat", source="user"
        ),
    ]

    cmd = await settle_claimed_batch(
        runtime.context, AgentState(messages=[SystemMessage(content="sys")]), tid, claimed
    )

    assert cmd.goto != END
