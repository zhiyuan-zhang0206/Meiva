from __future__ import annotations

import asyncio

import psycopg
from psycopg import AsyncConnection

from base.agents.history.harness_messages import (
    StoredPosition,
    aappend_keyed_timeline_items,
    aload_harness_timeline_items,
    aload_transcript_positions,
    aset_transcript_position,
    copy_harness_timeline,
)
from base.agents.history.timeline_item import TimelineItem


def _seed_agent(conn: psycopg.Connection) -> int:
    row = conn.execute("INSERT INTO agents DEFAULT VALUES RETURNING id").fetchone()
    assert row is not None
    agent_id = int(row[0])
    conn.execute(
        "INSERT INTO agents_meta (id, status, harness) VALUES (%s, 'idling', 'claude_code')",
        (agent_id,),
    )
    conn.commit()
    return agent_id


def _item(text: str) -> TimelineItem:
    return TimelineItem(item_id="x", kind="agent_chat", payload=text)


async def _append(
    url: str, agent_id: int, keyed: list[tuple[str, TimelineItem]]
) -> list[TimelineItem]:
    async with await AsyncConnection.connect(url) as conn:
        written = await aappend_keyed_timeline_items(
            conn, agent_id, "claude_code", keyed, projection_version=1
        )
        await conn.commit()
        return written


def test_a_keyed_item_read_twice_is_written_once_and_the_sequence_stays_contiguous(
    db_conn: psycopg.Connection, db_url: str
) -> None:
    agent_id = _seed_agent(db_conn)
    first = asyncio.run(_append(db_url, agent_id, [("k1", _item("one")), ("k2", _item("two"))]))
    assert [item.item_id for item in first] == ["h0.0", "h1.0"]
    again = asyncio.run(
        _append(
            db_url, agent_id, [("k2", _item("two")), ("k3", _item("three")), ("k1", _item("one"))]
        )
    )
    assert [(item.item_id, item.payload) for item in again] == [("h2.0", "three")]
    rows = db_conn.execute(
        "SELECT seq, source_key FROM agent_harness_messages WHERE agent_id = %s ORDER BY seq",
        (agent_id,),
    ).fetchall()
    assert rows == [(0, "k1"), (1, "k2"), (2, "k3")]


def test_the_same_key_may_exist_for_two_agents_and_unkeyed_rows_do_not_collide(
    db_conn: psycopg.Connection, db_url: str
) -> None:
    one, two = _seed_agent(db_conn), _seed_agent(db_conn)
    assert len(asyncio.run(_append(db_url, one, [("k", _item("a"))]))) == 1
    assert len(asyncio.run(_append(db_url, two, [("k", _item("b"))]))) == 1
    for _ in range(2):
        db_conn.execute(
            "INSERT INTO agent_harness_messages (agent_id, seq, harness, item) "
            "VALUES (%s, (SELECT COALESCE(MAX(seq) + 1, 0) FROM agent_harness_messages "
            "WHERE agent_id = %s), 'claude_code', '{}'::jsonb)",
            (one, one),
        )
    db_conn.commit()
    count = db_conn.execute(
        "SELECT count(*) FROM agent_harness_messages WHERE agent_id = %s", (one,)
    ).fetchone()
    assert count == (3,)


def test_a_forked_timeline_keeps_its_keys_so_the_clone_does_not_reproject_history(
    db_conn: psycopg.Connection, db_url: str
) -> None:
    source, clone = _seed_agent(db_conn), _seed_agent(db_conn)
    _ = asyncio.run(_append(db_url, source, [("k1", _item("one")), ("k2", _item("two"))]))
    with db_conn.cursor() as cur:
        assert copy_harness_timeline(cur, source, clone) == 2
    db_conn.commit()
    written = asyncio.run(_append(db_url, clone, [("k2", _item("two")), ("k3", _item("three"))]))
    assert [item.item_id for item in written] == ["h2.0"]


def test_positions_are_stored_per_record_and_upserted(
    db_conn: psycopg.Connection, db_url: str
) -> None:
    agent_id = _seed_agent(db_conn)

    async def run() -> tuple[
        dict[str, StoredPosition], dict[str, StoredPosition], dict[str, StoredPosition]
    ]:
        async with await AsyncConnection.connect(db_url) as conn:
            empty = await aload_transcript_positions(conn, agent_id, "s1")
            await aset_transcript_position(
                conn, agent_id, "s1", StoredPosition("main", "p1", 4), "2.1.322"
            )
            await aset_transcript_position(
                conn, agent_id, "s1", StoredPosition("agent-a", "q1", -1), None
            )
            await aset_transcript_position(
                conn, agent_id, "s1", StoredPosition("main", "p2", 9), None
            )
            await conn.commit()
            return (
                empty,
                await aload_transcript_positions(conn, agent_id, "s1"),
                await aload_transcript_positions(conn, agent_id, "other-session"),
            )

    empty, stored, other = asyncio.run(run())
    assert empty == {} and other == {}
    assert stored == {
        "main": StoredPosition("main", "p2", 9),
        "agent-a": StoredPosition("agent-a", "q1", -1),
    }
    version = db_conn.execute(
        "SELECT cli_version FROM agent_harness_transcript_positions "
        "WHERE agent_id = %s AND record = 'main'",
        (agent_id,),
    ).fetchone()
    assert version == ("2.1.322",)


def test_the_timeline_reader_still_returns_keyed_rows_in_append_order(
    db_conn: psycopg.Connection, db_url: str
) -> None:
    agent_id = _seed_agent(db_conn)
    _ = asyncio.run(_append(db_url, agent_id, [("a", _item("first")), ("b", _item("second"))]))

    async def read() -> list[str]:
        async with await AsyncConnection.connect(db_url) as conn:
            return [item.payload for item in await aload_harness_timeline_items(conn, agent_id)]

    assert asyncio.run(read()) == ["first", "second"]
