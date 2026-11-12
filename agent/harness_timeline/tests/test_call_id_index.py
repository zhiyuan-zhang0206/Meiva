# meiva:note
# The call-id lookup of the transcript writer is answered by an index on its own column, and keeping that column
# out of the expressions over `item` is what keeps the in-place revision of a live activity a heap-only update
# (the path every external harness takes with the transcript writer off).
# meiva:link agent/harness_timeline/README.md

from __future__ import annotations

from pathlib import Path
from typing import LiteralString, cast

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from agent.harness_timeline.store import PlannedTimelineItem, aappend_planned_timeline_items
from base.agents.history.harness_messages import (
    HARNESS_CALL_ITEMS_SQL,
    aappend_harness_timeline_items,
    aget_harness_call_item_ids,
    aupsert_harness_timeline_item,
)
from base.agents.history.timeline_item import TimelineItem


# meiva:note
# The tests that fill the table or analyze it work inside a transaction that is rolled back: the shared test database
# keeps no planner statistics of them (an `ANALYZE` that stayed would change the plans of the tests that run after it).
# The one test that needs committed updates drops the index it created.
async def _agent(conn: AsyncConnection) -> int:
    cursor = await conn.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
    row = await cursor.fetchone()
    assert row is not None
    agent_id = int(row[0])
    await conn.execute(
        "INSERT INTO agents_meta (id, status, harness) VALUES (%s, 'idling', 'claude_code')",
        (agent_id,),
    )
    return agent_id


def _call(call: str) -> TimelineItem:
    return TimelineItem(item_id="x", kind="agent_code", payload="Task", tool_use_id=call)


_REVISE = (
    "UPDATE agent_harness_messages SET item = jsonb_set(item, '{payload}', to_jsonb(%s::text)) "
    "WHERE agent_id = %s AND seq = %s"
)


# meiva:note
# The table's update counters of this database, flushed first (a backend reports its counters only at the end of a
# transaction, and not more often than once a second unless it is asked to).
async def _updates(conn: AsyncConnection) -> tuple[int, int]:
    await conn.execute("SELECT pg_stat_force_next_flush()")
    cursor = await conn.execute(
        "SELECT n_tup_hot_upd, n_tup_upd FROM pg_stat_user_tables WHERE relname = 'agent_harness_messages'"
    )
    row = await cursor.fetchone()
    assert row is not None
    return int(row[0]), int(row[1])


async def test_every_insert_path_fills_the_call_id_of_an_agent_code_item_and_only_of_those(
    aops_pool: AsyncConnectionPool,
) -> None:
    async with aops_pool.connection() as conn, conn.transaction(force_rollback=True):
        agent_id = await _agent(conn)
        chat = TimelineItem(item_id="x", kind="agent_chat", payload="hi")
        await aappend_harness_timeline_items(conn, agent_id, "claude_code", [_call("a"), chat])
        await aupsert_harness_timeline_item(
            conn,
            agent_id,
            "claude_code",
            _call("b").model_copy(update={"item_id": "h2.0", "activity_revision": 1}),
        )
        await aappend_planned_timeline_items(
            conn,
            agent_id,
            "claude_code",
            [PlannedTimelineItem("k", _call("c"), None)],
            calls={},
            projection_version=1,
        )
        found = await aget_harness_call_item_ids(conn, agent_id, ["a", "b", "c", "none"])
        cursor = await conn.execute(
            "SELECT count(*) FROM agent_harness_messages WHERE agent_id = %s AND call_id IS NOT NULL",
            (agent_id,),
        )
        filled = await cursor.fetchone()
    assert found == {"a": "h0.0", "b": "h2.0", "c": "h3.0"}
    assert filled == (3,)


async def test_revising_an_activity_in_place_stays_heap_only_and_an_expression_index_would_end_that(
    aops_pool: AsyncConnectionPool,
) -> None:
    async with aops_pool.connection() as conn:
        agent_id = await _agent(conn)
        try:
            await aappend_harness_timeline_items(
                conn, agent_id, "claude_code", [_call(f"call-{n}") for n in range(4)]
            )
            hot, total = await _revise_in_place(conn, agent_id, "with the call index")
            assert total == 20 and hot >= 17, (hot, total)
            await conn.execute(
                "CREATE INDEX control_item_expression "
                "ON agent_harness_messages (agent_id, (item->>'tool_use_id'))"
            )
            hot, total = await _revise_in_place(conn, agent_id, "with an expression over item")
            assert (hot, total) == (0, 20), "none of these updates is heap-only"
        finally:
            await conn.execute("DROP INDEX IF EXISTS control_item_expression")


# meiva:note
# Twenty revisions of rows in place, as autocommit statements like the production path, and how many of them were
# heap-only. A page with no room ends a heap-only chain now and then (the same table with no index at all did 19 of 20),
# so a test asks for practically all of them, against none with an index over an expression of `item`, the shape the
# call id column replaced.
async def _revise_in_place(conn: AsyncConnection, agent_id: int, label: str) -> tuple[int, int]:
    hot_before, total_before = await _updates(conn)
    for revision in range(20):
        await conn.execute(_REVISE, (f"{label} {revision}", agent_id, revision % 4))
    hot_after, total_after = await _updates(conn)
    return hot_after - hot_before, total_after - total_before


async def test_the_call_lookup_is_answered_by_the_call_index_not_a_scan(
    aops_pool: AsyncConnectionPool,
) -> None:
    async with aops_pool.connection() as conn, conn.transaction(force_rollback=True):
        agent_id = await _agent(conn)
        other = await _agent(conn)
        for owner in (agent_id, other):
            await conn.execute(
                "INSERT INTO agent_harness_messages (agent_id, seq, harness, item, call_id) "
                'SELECT %s, n, \'claude_code\', \'{"kind": "agent_chat", "payload": "x"}\'::jsonb, '
                "CASE WHEN n %% 10 = 0 THEN 'call-' || n END FROM generate_series(0, 4999) AS n",
                (owner,),
            )
        await conn.execute("ANALYZE agent_harness_messages")
        cursor = await conn.execute(
            "EXPLAIN " + HARNESS_CALL_ITEMS_SQL, (agent_id, ["call-10", "call-20", "call-30"])
        )
        plan = "\n".join(str(row[0]) for row in await cursor.fetchall())
    assert "idx_agent_harness_messages_call_id" in plan, plan
    assert "Seq Scan" not in plan, plan


_MIGRATION = next(
    Path(__file__).resolve().parents[3].glob("migrations/**/*_harness-call-item-index.sql")
)


async def test_the_migration_backfills_the_call_ids_of_rows_written_before_it_in_batches_and_replays(
    aops_pool: AsyncConnectionPool,
) -> None:
    async with aops_pool.connection() as conn, conn.transaction(force_rollback=True):
        agent_id = await _agent(conn)
        await conn.execute("DROP INDEX idx_agent_harness_messages_call_id")
        await conn.execute("ALTER TABLE agent_harness_messages DROP COLUMN call_id")
        await conn.execute(
            "INSERT INTO agent_harness_messages (agent_id, seq, harness, item) "
            "SELECT %s, n, 'claude_code', CASE WHEN n %% 3 = 0 "
            "THEN jsonb_build_object('kind', 'agent_code', 'tool_use_id', 'call-' || n) "
            "WHEN n %% 3 = 1 THEN jsonb_build_object('kind', 'agent_code', 'tool_use_id', '') "
            "ELSE jsonb_build_object('kind', 'agent_chat', 'tool_use_id', 'not-a-call') END "
            "FROM generate_series(0, 12999) AS n",
            (agent_id,),
        )
        for _ in range(2):
            await conn.execute(cast("LiteralString", _MIGRATION.read_text()))
            cursor = await conn.execute(
                "SELECT count(*), count(*) FILTER (WHERE call_id = 'call-' || seq) "
                "FROM agent_harness_messages WHERE call_id IS NOT NULL"
            )
            assert await cursor.fetchone() == (4334, 4334), "only agent_code items with a call id"
        found = await aget_harness_call_item_ids(conn, agent_id, ["call-0", "call-12999", "call-1"])
        cursor = await conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_agent_harness_messages_call'"
        )
        stale = await cursor.fetchall()
    assert found == {"call-0": "h0.0", "call-12999": "h12999.0"}
    assert stale == [], "an index of the earlier version under its name is dropped"
