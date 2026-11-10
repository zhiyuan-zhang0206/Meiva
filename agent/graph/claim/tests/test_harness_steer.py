import json
from pathlib import Path

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
from base.agents.tasks.delivery import supersede_task_assignments
from base.api_contracts.agent_contract import Harness
from base.clock import Clock
from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentSlices

from .claim_support import insert_inbound_kind


@pytest.mark.parametrize("harness", [Harness.CODEX, Harness.ANTIGRAVITY])
async def test_steer_consumes_only_after_ack_and_commits_timeline(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, harness: Harness
) -> None:
    agent = agent_row(db_conn)
    inbound_id = insert_inbound_kind(db_conn, agent, "more context", "chat", "user")
    ctx = MeivaContext(ops_pool=aops_pool, harness=harness)

    reserved = await reserve_harness_steer(ctx, agent)
    assert [row.id for row in reserved] == [inbound_id]
    assert [row.id for row in list_pending_inbounds(db_conn, agent)] == [inbound_id]
    assert load_harness_timeline_items(db_conn, agent) == []
    assert await claim_inbound_batch(aops_pool, agent, incarnation=None) == []

    item = TimelineItem(
        item_id="h0.0", kind="inbound_chat", inbound_id=inbound_id, payload="more context"
    )
    saved = await finish_harness_steer(ctx, agent, [inbound_id], accepted_items=[item])

    assert list_pending_inbounds(db_conn, agent) == []
    assert load_harness_timeline_items(db_conn, agent) == saved == [item]
    assert await recover_harness_steer(ctx, agent) == []


async def test_definite_steer_rejection_falls_back_to_claim(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent = agent_row(db_conn)
    first_id = insert_inbound_kind(db_conn, agent, "first prompt", "chat", "agent:7")
    second_id = insert_inbound_kind(db_conn, agent, "next prompt", "chat", "user")
    ctx = MeivaContext(ops_pool=aops_pool, harness=Harness.CODEX)

    reserved = await reserve_harness_steer(ctx, agent)
    assert [row.id for row in reserved] == [first_id, second_id]
    assert await finish_harness_steer(ctx, agent, [first_id, second_id], accepted_items=None) == []

    batch = await claim_inbound_batch(aops_pool, agent, incarnation=None)
    assert [row.id for row in batch] == [first_id, second_id]
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

    assert [row.id for row in await reserve_harness_steer(ctx, agent)] == [inbound_id]
    assert await reserve_harness_steer(ctx, agent) == []
    saved = await recover_harness_steer(ctx, agent, publish=False)

    assert await claim_inbound_batch(aops_pool, agent, incarnation=None) == []
    assert list_pending_inbounds(db_conn, agent) == []
    assert await recover_harness_steer(ctx, agent) == []
    (item,) = load_harness_timeline_items(db_conn, agent)
    assert saved == [item]
    assert item.kind == "system_marker"
    assert item.inbound_id == inbound_id
    assert "could not be confirmed" in item.payload


async def test_steer_reserves_entire_pending_batch_fifo_and_stops_at_barriers_or_controls(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, tmp_path: Path
) -> None:
    agent = agent_row(db_conn)
    ctx = MeivaContext(ops_pool=aops_pool, harness=Harness.CODEX)
    peer_id = insert_inbound_kind(db_conn, agent, "earlier automation", "chat", "agent:7")
    img_path = tmp_path / "shot.png"
    img_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
            "VALUES (%s, 'task assigned', 'system_note', 'system', %s::jsonb) RETURNING id",
            (
                agent,
                json.dumps(
                    {
                        "note_tag": "task",
                        "task_notification": True,
                        "delivery_resurrect": True,
                        "task_id": 42,
                    }
                ),
            ),
        )
        note_row = cur.fetchone()
        assert note_row is not None
        note_id: int = note_row[0]
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
            "VALUES (%s, 'check screenshot', 'chat', 'user', %s::jsonb) RETURNING id",
            (
                agent,
                json.dumps(
                    {
                        "image_uploads": [
                            {
                                "name": "shot.png",
                                "size": 8,
                                "content_type": "image/png",
                                "path": str(img_path),
                                "batch_id": "a" * 32,
                                "ordinal": 0,
                            }
                        ]
                    }
                ),
            ),
        )
        user_row = cur.fetchone()
        assert user_row is not None
        user_id: int = user_row[0]
    db_conn.commit()
    hb_id = insert_inbound_kind(db_conn, agent, "check in", "heartbeat", "system")
    compact_id = insert_inbound_kind(db_conn, agent, "later compact", "compact_request", "user")
    trailing_id = insert_inbound_kind(db_conn, agent, "after compact", "chat", "user")

    batch = await reserve_harness_steer(ctx, agent)
    assert [row.id for row in batch] == [peer_id, note_id, user_id, hb_id]
    with db_conn.cursor() as cur:
        supersede_task_assignments(cur, 42)
    db_conn.commit()

    saved = await finish_harness_steer(
        ctx,
        agent,
        [peer_id, note_id, user_id, hb_id],
        accepted_items=[
            TimelineItem(
                item_id="h0.0",
                kind="inbound_chat",
                source="agent:7",
                inbound_id=peer_id,
                payload="earlier automation",
            ),
            TimelineItem(
                item_id="h1.0", kind="system_marker", source="task", payload="task assigned"
            ),
            TimelineItem(
                item_id="h2.0",
                kind="inbound_chat",
                source="user",
                inbound_id=user_id,
                payload="check screenshot",
            ),
            TimelineItem(
                item_id="h3.0", kind="system_marker", source="heartbeat", payload="check in"
            ),
        ],
    )
    assert len(saved) == 4
    assert await reserve_harness_steer(ctx, agent) == []
    remaining = await claim_inbound_batch(aops_pool, agent, incarnation=None)
    assert [row.id for row in remaining] == [compact_id, trailing_id]

    insert_inbound_kind(db_conn, agent, "before cancel", "chat", "user")
    insert_inbound_kind(db_conn, agent, "", "cancel", "user")
    assert await reserve_harness_steer(ctx, agent) == []


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
        item_id="h0.0", kind="inbound_chat", inbound_id=inbound_id, payload="initial prompt"
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
