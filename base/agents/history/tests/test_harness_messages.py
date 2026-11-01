from __future__ import annotations

import psycopg

from base.agents.history.harness_messages import (
    aappend_harness_timeline_items,
    aget_next_harness_message_seq,
    aload_harness_message_count,
    aload_harness_timeline_items,
    append_harness_timeline_items,
    get_next_harness_message_seq,
    load_harness_message_count,
    load_harness_timeline_items,
)
from base.agents.history.timeline_item import TimelineItem


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

    assert [it.item_id for it in persisted] == ["0.0", "1.0"]
    assert get_next_harness_message_seq(db_conn, agent_id) == 2
    assert load_harness_message_count(db_conn, agent_id) == 2

    loaded = load_harness_timeline_items(db_conn, agent_id)
    assert [it.payload for it in loaded] == ["First message", "Second message"]

    paged = load_harness_timeline_items(db_conn, agent_id, before_seq=1, limit=1)
    assert [it.item_id for it in paged] == ["0.0"]


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

        assert [it.item_id for it in persisted] == ["0.0", "1.0"]
        assert await aget_next_harness_message_seq(aconn, agent_id) == 2
        assert await aload_harness_message_count(aconn, agent_id) == 2
        loaded = await aload_harness_timeline_items(aconn, agent_id, before_seq=1)
        assert [it.payload for it in loaded] == ["async hello"]
