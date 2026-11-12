# meiva:note
# The reply progress the writer makes in the transaction of its first commit: at the end of the timeline as it was
# before that commit, so what the agent already had (a fork's copy of its parent's timeline, rows the event path wrote)
# is never among the replies the effects owe, and the replies the writer commits after it are, until they are confirmed.
# meiva:link agent/harness_timeline/README.md

from __future__ import annotations

import psycopg
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from agent.harness_timeline.reply_progress import aconfirm_replies, aload_unconfirmed_replies
from agent.harness_timeline.tests.support import (
    BASIC,
    SUBS,
    ScriptedReader,
    make_agent,
    make_inbound_row,
    make_writer,
    send_input,
)
from base.agents.history.harness_messages import aappend_keyed_timeline_items, copy_harness_timeline
from base.agents.history.timeline_item import TimelineItem
from base.lm.harness.claude.transcript.tests.support import entries_of


async def _owed(pool: AsyncConnectionPool, agent_id: int) -> list[int] | None:
    async with pool.connection() as conn:
        owed = await aload_unconfirmed_replies(conn, agent_id, limit=100)
    return None if owed is None else [reply.seq for reply in owed.replies]


async def test_a_writer_with_no_commit_yet_owes_nothing_and_its_first_commit_owes_its_own_replies(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    row = await make_inbound_row(aops_pool, agent_id)
    reader = ScriptedReader({"main": entries_of("basic")})
    writer = make_writer(aops_pool, agent_id, incarnation, reader, [])
    send_input(writer, BASIC, "k1", row)
    assert await _owed(aops_pool, agent_id) is None
    reader.visible["main"] = len(reader.records_data["main"])
    assert await writer.read_now()
    owed = await _owed(aops_pool, agent_id)
    assert owed is not None and len(owed) == 1, "the turn's one reply"
    async with aops_pool.connection() as conn:
        await aconfirm_replies(conn, agent_id, owed[0])
    assert await _owed(aops_pool, agent_id) == []


async def test_a_forks_first_commit_does_not_owe_the_history_it_copied(
    aops_pool: AsyncConnectionPool, db_conn: psycopg.Connection
) -> None:
    parent, parent_inc = await make_agent(aops_pool)
    row = await make_inbound_row(aops_pool, parent)
    reader = ScriptedReader({"main": entries_of("basic")})
    reader.visible["main"] = len(reader.records_data["main"])
    writer = make_writer(aops_pool, parent, parent_inc, reader, [])
    send_input(writer, BASIC, "k1", row)
    assert await writer.read_now()
    parent_owed = await _owed(aops_pool, parent)
    assert parent_owed is not None and len(parent_owed) == 1

    child, child_inc = await make_agent(aops_pool)
    with db_conn.cursor() as cur:
        copied = copy_harness_timeline(cur, parent, child)
    db_conn.commit()
    assert await _owed(aops_pool, child) is None, "a fork has no progress until its writer commits"
    child_reader = ScriptedReader({"main": entries_of("subagents")})
    child_reader.visible["main"] = len(child_reader.records_data["main"])
    child_writer = make_writer(aops_pool, child, child_inc, child_reader, [])
    send_input(child_writer, SUBS, "k1", await make_inbound_row(aops_pool, child))
    assert await child_writer.read_now()
    owed = await _owed(aops_pool, child)
    assert owed and min(owed) >= copied, "only rows after the copied history"


async def test_an_agent_whose_timeline_the_event_path_wrote_does_not_owe_those_rows(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    earlier = TimelineItem(item_id="x", kind="agent_chat", payload="earlier", user_facing=True)
    async with aops_pool.connection() as conn:
        _ = await aappend_keyed_timeline_items(
            conn, agent_id, "claude_code", [("old:1", earlier)], projection_version=1
        )
    row = await make_inbound_row(aops_pool, agent_id)
    reader = ScriptedReader({"main": entries_of("subagents")})
    reader.visible["main"] = len(reader.records_data["main"])
    writer = make_writer(aops_pool, agent_id, incarnation, reader, [])
    send_input(writer, SUBS, "k1", row)
    assert await writer.read_now()
    owed = await _owed(aops_pool, agent_id)
    assert owed and 0 not in owed


_READS = (
    "SELECT COALESCE(sum(pg_stat_get_xact_tuples_returned(oid) + pg_stat_get_xact_tuples_fetched(oid) "
    "+ pg_stat_get_xact_blocks_fetched(oid)), 0)::bigint FROM pg_class "
    "WHERE relnamespace = 'public'::regnamespace AND relkind IN ('r', 'i')"
)
SLACK = 30


async def _reads(conn: AsyncConnection) -> int:
    cursor = await conn.execute(_READS)
    row = await cursor.fetchone()
    assert row is not None
    return int(str(row[0]))


async def _pass_cost(pool: AsyncConnectionPool, agent_id: int) -> int:
    async with pool.connection() as conn, conn.transaction():
        before = await _reads(conn)
        _ = await aload_unconfirmed_replies(conn, agent_id, limit=100)
        return await _reads(conn) - before


async def _history(pool: AsyncConnectionPool, agent_id: int, rows: int, *, confirmed: bool) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO agent_harness_messages (agent_id, seq, harness, item, source_key) "
            "SELECT %s, g, 'claude_code', jsonb_build_object('item_id', 'h' || g || '.0', "
            "'kind', 'agent_code', 'payload', 'x'), 'k' || g FROM generate_series(0, %s) AS g",
            (agent_id, rows - 1),
        )
        await conn.execute(
            "INSERT INTO agent_harness_timeline_progress (agent_id, replies_seq) VALUES (%s, %s)",
            (agent_id, rows - 1 if confirmed else -1),
        )
        await conn.execute(
            "INSERT INTO agent_harness_messages (agent_id, seq, harness, item, source_key) "
            "VALUES (%s, %s, 'claude_code', "
            '\'{"item_id": "x", "kind": "agent_chat", "payload": "r"}\'::jsonb, \'tail\')',
            (agent_id, rows),
        )


# meiva:note
# The cost of what the effects read after a commit, taken from the database's own counters (the tuples returned and
# fetched and the blocks fetched of the tables and indexes, inside one transaction, the way the writer's linear tests
# count): it does not grow with the history behind the progress, however many of its rows are not replies, and after a
# pass that has confirmed a long stretch of non-reply rows the next pass does not read that stretch again.
async def test_what_a_pass_reads_does_not_grow_with_the_history_behind_the_progress(
    aops_pool: AsyncConnectionPool,
) -> None:
    costs: dict[tuple[int, str], int] = {}
    for rows in (100, 5_000):
        behind, _ = await make_agent(aops_pool)
        await _history(aops_pool, behind, rows, confirmed=True)
        costs[(rows, "history behind")] = await _pass_cost(aops_pool, behind)
        gap, _ = await make_agent(aops_pool)
        await _history(aops_pool, gap, rows, confirmed=False)
        _ = await _pass_cost(aops_pool, gap)
        async with aops_pool.connection() as conn:
            await aconfirm_replies(conn, gap, rows)
        costs[(rows, "after a confirmed gap")] = await _pass_cost(aops_pool, gap)
    for label in ("history behind", "after a confirmed gap"):
        assert costs[(5_000, label)] <= costs[(100, label)] + SLACK, (label, costs)
