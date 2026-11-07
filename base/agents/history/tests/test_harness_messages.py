from __future__ import annotations

import asyncio
from uuid import UUID

import psycopg
import pytest

from base.agents.history.harness_messages import (
    aappend_harness_timeline_items,
    aget_next_harness_message_seq,
    aload_harness_message_count,
    aload_harness_timeline_items,
    append_harness_timeline_items,
    aupsert_harness_timeline_item,
    get_next_harness_message_seq,
    load_harness_message_count,
    load_harness_timeline_items,
)
from base.agents.history.timeline_item import TimelineItem
from base.native_process.runtime_incarnation import RuntimeIncarnation, StaleIncarnationError


def _seed_agent(conn: psycopg.Connection) -> int:
    row = conn.execute("INSERT INTO agents DEFAULT VALUES RETURNING id").fetchone()
    assert row is not None
    agent_id = int(row[0])
    conn.execute(
        "INSERT INTO agents_meta (id, status, harness) VALUES (%s, 'idling', 'codex')",
        (agent_id,),
    )
    conn.commit()
    return agent_id


def test_sync_harness_messages_lifecycle(db_conn: psycopg.Connection) -> None:
    agent_id = _seed_agent(db_conn)
    assert get_next_harness_message_seq(db_conn, agent_id) == 0
    assert load_harness_message_count(db_conn, agent_id) == 0
    assert load_harness_timeline_items(db_conn, agent_id) == []

    items = [
        TimelineItem(
            item_id="placeholder",
            kind="inbound_chat",
            payload="First message",
            created_at="2026-11-01T12:00:00Z",
        ),
        TimelineItem(
            item_id="placeholder",
            kind="agent_chat",
            payload="Second message",
            created_at="2026-11-01T12:00:01Z",
        ),
    ]
    persisted = append_harness_timeline_items(db_conn, agent_id, "codex", items)
    db_conn.commit()

    assert [it.item_id for it in persisted] == ["h0.0", "h1.0"]
    assert get_next_harness_message_seq(db_conn, agent_id) == 2
    assert load_harness_message_count(db_conn, agent_id) == 2

    loaded = load_harness_timeline_items(db_conn, agent_id)
    assert [it.payload for it in loaded] == ["First message", "Second message"]

    paged = load_harness_timeline_items(db_conn, agent_id, before_seq=1, limit=1)
    assert [it.item_id for it in paged] == ["h0.0"]


async def test_async_harness_messages_lifecycle(db_conn: psycopg.Connection) -> None:
    agent_id = _seed_agent(db_conn)
    async with await psycopg.AsyncConnection.connect(db_conn.info.dsn) as aconn:
        assert await aget_next_harness_message_seq(aconn, agent_id) == 0
        assert await aload_harness_message_count(aconn, agent_id) == 0

        persisted = await aappend_harness_timeline_items(
            aconn,
            agent_id,
            "claude_code",
            [
                TimelineItem(
                    item_id="placeholder",
                    kind="inbound_chat",
                    payload="async hello",
                    created_at="2026-11-01T12:00:00Z",
                ),
                TimelineItem(
                    item_id="placeholder",
                    kind="agent_chat",
                    payload="async reply",
                    created_at="2026-11-01T12:00:02Z",
                ),
            ],
        )
        await aconn.commit()

        assert [it.item_id for it in persisted] == ["h0.0", "h1.0"]
        assert await aget_next_harness_message_seq(aconn, agent_id) == 2
        assert await aload_harness_message_count(aconn, agent_id) == 2
        loaded = await aload_harness_timeline_items(aconn, agent_id, before_seq=1)
        assert [it.payload for it in loaded] == ["async hello"]


async def test_activity_updates_only_its_own_revisioned_row(db_conn: psycopg.Connection) -> None:
    agent_id = _seed_agent(db_conn)
    legacy = TimelineItem(item_id="h0.0", kind="agent_chat", payload="old")
    append_harness_timeline_items(db_conn, agent_id, "codex", [legacy])
    db_conn.commit()

    first = TimelineItem(
        item_id="h1.0", kind="agent_reasoning", payload="start", activity_revision=1
    )
    latest = first.model_copy(update={"activity_revision": 2, "payload": "done"})
    async with await psycopg.AsyncConnection.connect(db_conn.info.dsn, autocommit=True) as conn:
        assert await aupsert_harness_timeline_item(conn, agent_id, "codex", first) == first
        assert await aupsert_harness_timeline_item(conn, agent_id, "codex", latest) == latest
        assert await aupsert_harness_timeline_item(conn, agent_id, "codex", latest) == latest
        for harness, item in (
            ("codex", first),
            ("codex", latest.model_copy(update={"payload": "conflicting retry"})),
            ("codex", latest.model_copy(update={"item_id": "h0.0"})),
            ("codex", latest.model_copy(update={"item_id": "h3.0"})),
            ("codex", latest.model_copy(update={"activity_revision": 3, "kind": "agent_chat"})),
            ("claude_code", latest.model_copy(update={"activity_revision": 3})),
        ):
            assert await aupsert_harness_timeline_item(conn, agent_id, harness, item) is None
        assert await aload_harness_timeline_items(conn, agent_id) == [legacy, latest]
        assert await aget_next_harness_message_seq(conn, agent_id) == 2


async def test_activity_requires_original_runtime_owner(db_conn: psycopg.Connection) -> None:
    agent_id = _seed_agent(db_conn)
    original = RuntimeIncarnation(agent_id, UUID(int=1), UUID(int=2))
    db_conn.execute(
        "UPDATE agents_meta SET runtime_generation = %s, runtime_owner = %s WHERE id = %s",
        (original.generation, original.owner, agent_id),
    )
    db_conn.commit()
    item = TimelineItem(item_id="h0.0", kind="agent_code", payload="start", activity_revision=1)
    async with await psycopg.AsyncConnection.connect(db_conn.info.dsn, autocommit=True) as conn:
        assert (
            await aupsert_harness_timeline_item(conn, agent_id, "codex", item, incarnation=original)
            == item
        )
        db_conn.execute(
            "UPDATE agents_meta SET runtime_owner = %s WHERE id = %s", (UUID(int=3), agent_id)
        )
        db_conn.commit()
        for item_id in ("h0.0", "h1.0"):
            update = item.model_copy(update={"item_id": item_id, "activity_revision": 2})
            with pytest.raises(StaleIncarnationError, match="stale harness activity write"):
                await aupsert_harness_timeline_item(
                    conn, agent_id, "codex", update, incarnation=original
                )
        assert await aload_harness_timeline_items(conn, agent_id) == [item]


async def test_concurrent_activity_creates_only_one_version(db_conn: psycopg.Connection) -> None:
    agent_id = _seed_agent(db_conn)
    first = TimelineItem(item_id="h0.0", kind="agent_code", payload="first", activity_revision=1)
    competing = first.model_copy(update={"payload": "competing"})
    async with (
        await psycopg.AsyncConnection.connect(db_conn.info.dsn, autocommit=True) as conn_a,
        await psycopg.AsyncConnection.connect(db_conn.info.dsn, autocommit=True) as conn_b,
    ):
        results = await asyncio.gather(
            aupsert_harness_timeline_item(conn_a, agent_id, "codex", first),
            aupsert_harness_timeline_item(conn_b, agent_id, "codex", competing),
        )
        stored = await aload_harness_timeline_items(conn_a, agent_id)
    assert len(stored) == 1
    assert results.count(None) == 1
    assert stored[0] in results
