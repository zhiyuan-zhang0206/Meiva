# meiva:note
# Passive inbox rows must not prevent control claims or bypass non-passive FIFO input.
# meiva:link agent/graph/docs/harness-foreground.md

import json
from dataclasses import replace

import psycopg
import pytest
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

from agent.db import claim_inbound_batch
from agent.graph.claim.harness_turn import harness_turn_node
from agent.graph.claim.tests.harness_support import (
    FakeEngine,
    RuntimeFixture,
    input_key,
    insert_row,
    row_status,
)
from agent.graph.claim.tests.harness_support import harness_runtime as harness_runtime
from agent.graph.claim.tests.harness_support import hosted_resources as hosted_resources
from agent.graph.claim.tests.test_harness_turn import config
from agent.ownership.tests.ownership_support import admit_owner, agent_row
from agent.state import AgentState
from base.db.code_version_gate import ProcessDbGate


async def test_the_claim_leaves_deliverable_rows_to_delivery_and_takes_leading_controls(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    control = insert_row(db_conn, agent_id, "resurrect", "system", "")
    chat = insert_row(db_conn, agent_id, "chat", "user", "after the control")
    later = insert_row(db_conn, agent_id, "interrupt", "user", "")
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)

    batch = await claim_inbound_batch(
        aops_pool, agent_id, deliverable_by_harness=True, incarnation=owner
    )

    assert [item.id for item in batch] == [control], "stops before the first deliverable row"
    assert [row_status(db_conn, i) for i in (chat, later)] == ["pending", "pending"]
    again = await claim_inbound_batch(
        aops_pool, agent_id, deliverable_by_harness=True, incarnation=owner
    )
    assert again == [], "the chat is next, and it is delivery's"


@pytest.mark.parametrize("kind", ["resurrect", "interrupt", "restart", "terminate"])
@pytest.mark.parametrize("note_tag", ["lifecycle_restart", "lifecycle_interrupt"])
async def test_passive_notes_do_not_block_control_claims(
    kind: str,
    note_tag: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    note = insert_row(
        db_conn,
        agent_id,
        "system_note",
        "system",
        "passive recovery note",
        payload=json.dumps({"passive": True, "note_tag": note_tag}),
    )
    control = insert_row(db_conn, agent_id, kind, "system", "")
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    batch = await claim_inbound_batch(
        aops_pool, agent_id, deliverable_by_harness=True, incarnation=owner
    )
    assert [item.id for item in batch] == [control]
    assert row_status(db_conn, control) == (
        "claimed" if kind in ("restart", "terminate") else "done"
    )
    assert batch[0].durable_lifecycle == (kind in ("restart", "terminate"))
    assert row_status(db_conn, note) == "pending"


async def test_passive_note_resurrect_chat_claim_leaves_delivery_pending(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    note = insert_row(
        db_conn,
        agent_id,
        "system_note",
        "system",
        "passive recovery note",
        payload='{"passive": true, "note_tag": "lifecycle_restart"}',
    )
    control = insert_row(db_conn, agent_id, "resurrect", "system", "")
    chat = insert_row(db_conn, agent_id, "chat", "user", "continue work")
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    batch = await claim_inbound_batch(
        aops_pool, agent_id, deliverable_by_harness=True, incarnation=owner
    )
    assert [item.id for item in batch] == [control]
    assert [row_status(db_conn, i) for i in (note, control, chat)] == ["pending", "done", "pending"]


async def test_non_passive_input_still_blocks_control_behind_a_passive_note(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
    harness_runtime: RuntimeFixture,
) -> None:
    agent_id = agent_row(db_conn)
    chat = insert_row(db_conn, agent_id, "chat", "user", "earlier message")
    note = insert_row(
        db_conn,
        agent_id,
        "system_note",
        "system",
        "passive recovery note",
        payload='{"passive": true, "note_tag": "lifecycle_interrupt"}',
    )
    control = insert_row(db_conn, agent_id, "resurrect", "system", "")
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    assert (
        await claim_inbound_batch(
            aops_pool, agent_id, deliverable_by_harness=True, incarnation=owner
        )
        == []
    )
    engine = FakeEngine()
    base = harness_runtime(engine, agent_id=agent_id)
    rt = Runtime(context=replace(base.context, original_incarnation=owner))
    await harness_turn_node(AgentState(), rt, config(agent_id))
    [[sent]] = engine.delivered
    assert sent.key == input_key(chat)
    assert "earlier message" in sent.text and "passive recovery note" in sent.text
    assert [row_status(db_conn, i) for i in (chat, note, control)] == ["done", "done", "pending"]
    batch = await claim_inbound_batch(
        aops_pool,
        agent_id,
        deliverable_by_harness=True,
        incarnation=rt.context.original_incarnation,
    )
    assert [item.id for item in batch] == [control]
