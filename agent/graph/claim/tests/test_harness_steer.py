import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.db import claim_inbound_batch
from agent.graph.claim.harness_input import commit_harness_input, retire_unconfirmed_harness_input
from agent.graph.claim.harness_steer import (
    finish_harness_steer,
    recover_harness_steer,
    reserve_harness_steer,
)
from agent.ownership.tests.ownership_support import agent_row
from base.agents.context import MeivaContext
from base.agents.history.harness_messages import load_harness_timeline_items
from base.agents.history.timeline_item import TimelineItem
from base.agents.inbox.rows import list_pending_inbounds
from base.api_contracts.agent_contract import Harness
from base.clock import Clock
from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentSlices

from .claim_support import insert_inbound_kind


async def test_steer_consumes_only_after_ack_and_commits_timeline(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent = agent_row(db_conn)
    inbound_id = insert_inbound_kind(db_conn, agent, "more context", "chat", "user")
    ctx = MeivaContext(ops_pool=aops_pool, harness=Harness.CODEX)

    reserved = await reserve_harness_steer(ctx, agent)
    assert reserved is not None
    assert reserved.id == inbound_id
    assert [row.id for row in list_pending_inbounds(db_conn, agent)] == [inbound_id]
    assert load_harness_timeline_items(db_conn, agent) == []
    assert await claim_inbound_batch(aops_pool, agent, incarnation=None) == []

    item = TimelineItem(
        item_id="0.0", kind="inbound_chat", inbound_id=inbound_id, payload="more context"
    )
    saved = await finish_harness_steer(ctx, agent, inbound_id, accepted_items=[item])

    assert list_pending_inbounds(db_conn, agent) == []
    assert load_harness_timeline_items(db_conn, agent) == saved == [item]
    assert await recover_harness_steer(ctx, agent) == []


async def test_definite_steer_rejection_falls_back_to_claim(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent = agent_row(db_conn)
    inbound_id = insert_inbound_kind(db_conn, agent, "next prompt", "chat", "user")
    ctx = MeivaContext(ops_pool=aops_pool, harness=Harness.CODEX)

    assert await reserve_harness_steer(ctx, agent) is not None
    assert await finish_harness_steer(ctx, agent, inbound_id, accepted_items=None) == []

    batch = await claim_inbound_batch(aops_pool, agent, incarnation=None)
    assert [row.id for row in batch] == [inbound_id]
    assert load_harness_timeline_items(db_conn, agent) == []


async def test_unconfirmed_steer_retires_with_uncertainty_and_no_replay(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent = agent_row(db_conn)
    inbound_id = insert_inbound_kind(db_conn, agent, "possibly delivered", "chat", "user")
    ctx = MeivaContext(
        ops_pool=aops_pool,
        harness=Harness.CODEX,
        agent=AgentSlices.resolve(default_reader=defaults()),
        clock_factory=Clock.from_settings,
    )

    assert await reserve_harness_steer(ctx, agent) is not None
    assert await reserve_harness_steer(ctx, agent) is None
    saved = await recover_harness_steer(ctx, agent, publish=False)

    assert await claim_inbound_batch(aops_pool, agent, incarnation=None) == []
    assert list_pending_inbounds(db_conn, agent) == []
    assert await recover_harness_steer(ctx, agent) == []
    (item,) = load_harness_timeline_items(db_conn, agent)
    assert saved == [item]
    assert item.kind == "system_marker"
    assert item.inbound_id == inbound_id
    assert "could not be confirmed" in item.payload


async def test_steer_waits_behind_older_automation_and_pending_cancel(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent = agent_row(db_conn)
    ctx = MeivaContext(ops_pool=aops_pool, harness=Harness.CODEX)
    insert_inbound_kind(db_conn, agent, "earlier automation", "chat", "agent:7")
    insert_inbound_kind(db_conn, agent, "followup", "chat", "user")

    assert await reserve_harness_steer(ctx, agent) is None
    assert len(await claim_inbound_batch(aops_pool, agent, incarnation=None)) == 2

    insert_inbound_kind(db_conn, agent, "before cancel", "chat", "user")
    insert_inbound_kind(db_conn, agent, "", "cancel", "user")

    assert await reserve_harness_steer(ctx, agent) is None


async def test_external_initial_claim_remains_visible_until_provider_receipt(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent = agent_row(db_conn)
    _ = db_conn.execute("UPDATE agents_meta SET harness='codex' WHERE id=%s", (agent,))
    inbound_id = insert_inbound_kind(db_conn, agent, "initial prompt", "chat", "user")
    ctx = MeivaContext(ops_pool=aops_pool, harness=Harness.CODEX)

    assert [row.id for row in await claim_inbound_batch(aops_pool, agent, incarnation=None)] == [
        inbound_id
    ]
    assert [row.id for row in list_pending_inbounds(db_conn, agent)] == [inbound_id]
    assert load_harness_timeline_items(db_conn, agent) == []
    item = TimelineItem(
        item_id="0.0", kind="inbound_chat", inbound_id=inbound_id, payload="initial prompt"
    )
    staged = {"timeline_items": [item.model_dump(mode="json")], "claimed_chat_ids": [inbound_id]}
    with pytest.raises(RuntimeError, match="harness input claim changed"):
        await commit_harness_input(
            ctx, agent, {**staged, "claimed_chat_ids": [inbound_id, inbound_id + 1]}
        )
    assert [row.id for row in list_pending_inbounds(db_conn, agent)] == [inbound_id]
    assert load_harness_timeline_items(db_conn, agent) == []
    saved = await commit_harness_input(ctx, agent, staged)

    assert await retire_unconfirmed_harness_input(ctx, agent, staged) == []
    assert list_pending_inbounds(db_conn, agent) == []
    assert load_harness_timeline_items(db_conn, agent) == saved == [item]


async def test_unconfirmed_initial_claim_retires_with_linked_uncertainty(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent = agent_row(db_conn)
    _ = db_conn.execute("UPDATE agents_meta SET harness='codex' WHERE id=%s", (agent,))
    inbound_id = insert_inbound_kind(db_conn, agent, "initial prompt", "chat", "user")
    ctx = MeivaContext(
        ops_pool=aops_pool,
        harness=Harness.CODEX,
        agent=AgentSlices.resolve(default_reader=defaults()),
        clock_factory=Clock.from_settings,
    )

    assert [row.id for row in await claim_inbound_batch(aops_pool, agent, incarnation=None)] == [
        inbound_id
    ]
    assert [row.id for row in list_pending_inbounds(db_conn, agent)] == [inbound_id]

    saved = await retire_unconfirmed_harness_input(ctx, agent, {"claimed_chat_ids": [inbound_id]})

    assert (
        await retire_unconfirmed_harness_input(ctx, agent, {"claimed_chat_ids": [inbound_id]}) == []
    )
    assert list_pending_inbounds(db_conn, agent) == []
    assert await claim_inbound_batch(aops_pool, agent, incarnation=None) == []
    (item,) = load_harness_timeline_items(db_conn, agent)
    assert saved == [item]
    assert item.kind == "system_marker"
    assert item.inbound_id == inbound_id
    assert "could not be confirmed" in item.payload
