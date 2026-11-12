from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from agent.harness_timeline.store import (
    PlannedTimelineItem,
    StoredPosition,
    aappend_planned_timeline_items,
    aappend_transcript_rows,
    aload_transcript_positions,
    aload_transcript_rows,
    aset_transcript_position,
    aset_transcript_positions,
)
from base.agents.history.timeline_item import TimelineItem
from base.lm.harness.claude.transcript.reader import ClaudeRecordReader
from base.lm.harness.claude.transcript.tests.support import FIXTURES, now_ns, rows

SESSION = "11111111-2222-3333-4444-555555555555"


async def _agent(pool: AsyncConnectionPool) -> int:
    async with pool.connection() as conn:
        cursor = await conn.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        row = await cursor.fetchone()
        assert row is not None
        agent_id = int(row[0])
        await conn.execute(
            "INSERT INTO agents_meta (id, status, harness) VALUES (%s, 'idling', 'claude_code')",
            (agent_id,),
        )
        return agent_id


async def test_stored_rows_are_appended_once_and_read_back_in_order_after_an_ordinal(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id = await _agent(aops_pool)
    batch = [("main", 0, {"type": "user"}), ("main", 2, {"type": "assistant"}), ("a", 0, {"x": 1})]
    async with aops_pool.connection() as conn:
        assert await aappend_transcript_rows(conn, agent_id, SESSION, batch) == 3
        assert await aappend_transcript_rows(conn, agent_id, SESSION, batch) == 0
        assert await aappend_transcript_rows(conn, agent_id, "other", batch[:1]) == 1
        loaded = await aload_transcript_rows(
            conn, agent_id, SESSION, "main", after_ordinal=-1, limit=10
        )
        later = await aload_transcript_rows(
            conn, agent_id, SESSION, "main", after_ordinal=0, limit=10
        )
        limited = await aload_transcript_rows(
            conn, agent_id, SESSION, "main", after_ordinal=-1, limit=1
        )
    assert loaded == [(0, {"type": "user"}), (2, {"type": "assistant"})]
    assert later == [(2, {"type": "assistant"})] and limited == loaded[:1]


def _long_session(turns: int) -> list[str]:
    lines = (FIXTURES / "basic" / "main.jsonl").read_text().splitlines()
    out: list[str] = []
    for turn in range(turns):
        for line in lines:
            row = json.loads(line)
            if "uuid" in row:
                row["uuid"] = f"{row['uuid']}-{turn}"
            message = row.get("message")
            if isinstance(message, dict) and "id" in message:
                message["id"] = f"{message['id']}-{turn}"
            out.append(json.dumps(row))
            # meiva:note
            # The CLI's per-request bookkeeping carries the whole request each time (it grows with the
            # conversation) and an account identifier: none of it may reach the database.
            for kind in ("api-request", "api-request-shape", "api-request-blob"):
                blob = {"type": kind, "uuid": f"{kind}-{turn}", "accountUuid": "acct", "n": turn}
                blob["body"] = "r" * (200 * (turn + 1))
                out.append(json.dumps(blob))
    return out


_TABLES = (
    "agent_harness_transcript_rows",
    "agent_harness_transcript_rows_pkey",
    "agent_harness_transcript_positions",
    "agent_harness_transcript_positions_pkey",
)


# meiva:note
# Tuples and blocks this transaction has read from the transcript tables so far.
async def _database_reads(conn: AsyncConnection) -> int:
    cursor = await conn.execute(
        "SELECT COALESCE(sum(pg_stat_get_xact_tuples_returned(oid) + "
        "pg_stat_get_xact_tuples_fetched(oid) + pg_stat_get_xact_blocks_fetched(oid)), 0) "
        "FROM pg_class WHERE relname = ANY(%s)",
        (list(_TABLES),),
    )
    row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


# meiva:note
# What the database reads per batch (tuples and blocks of the transcript tables, within the batch's own
# transaction) does not grow with what is already stored: the later full batches read no more than the first.
def _assert_database_reads_flat(db_reads: list[int]) -> None:
    full = db_reads[:-1]
    assert max(full[len(full) // 2 :]) <= 1.5 * max(full[:2]) + 20, db_reads


# meiva:note
# The row types that must stay out of the database are not there, though the file is full of them.
async def _assert_bookkeeping_stayed_out(pool: AsyncConnectionPool, agent_id: int) -> None:
    async with pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT count(*) FROM agent_harness_transcript_rows "
            "WHERE agent_id = %s AND (row->>'type' LIKE 'api-request%%' OR row::text LIKE '%%acct%%')",
            (agent_id,),
        )
        assert await cursor.fetchone() == (0,)


@dataclass
class _Series:
    written: list[int] = field(default_factory=list[int])
    stored_bytes: list[int] = field(default_factory=list[int])
    read_bytes: list[int] = field(default_factory=list[int])
    file_bytes: list[int] = field(default_factory=list[int])
    db_reads: list[int] = field(default_factory=list[int])


async def _assert_linear(
    pool: AsyncConnectionPool, agent_id: int, series: _Series, *, file_lines: int
) -> None:
    async with pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT count(*), sum(pg_column_size(row)) FROM agent_harness_transcript_rows "
            "WHERE agent_id = %s",
            (agent_id,),
        )
        total = await cursor.fetchone()
        positions = await (
            await conn.execute(
                "SELECT count(*) FROM agent_harness_transcript_positions WHERE agent_id = %s",
                (agent_id,),
            )
        ).fetchone()
    written, stored_bytes = series.written, series.stored_bytes
    assert total is not None and positions == (1,)
    assert int(total[0]) == sum(written) and sum(written) > 0
    assert max(written) <= 1.2 * min(written[:-1] or written) + 5
    assert max(stored_bytes[:-1]) <= 1.2 * min(stored_bytes[:-1])
    assert int(total[1]) / int(total[0]) < 4 * (sum(stored_bytes) / sum(written))
    assert all(
        read <= 1.5 * size + 20_000
        for read, size in zip(series.read_bytes, series.file_bytes, strict=True)
    )
    assert len(written) >= 6
    _assert_database_reads_flat(series.db_reads)
    await _assert_bookkeeping_stayed_out(pool, agent_id)
    assert sum(written) < file_lines // 2


async def test_a_long_session_costs_linear_storage_and_constant_work_per_batch(
    aops_pool: AsyncConnectionPool, tmp_path: Path
) -> None:
    session = rows("basic")[0]["sessionId"]
    assert isinstance(session, str)
    target = tmp_path / "projects" / "p" / f"{session}.jsonl"
    target.parent.mkdir(parents=True)
    target.write_text("")
    lines = _long_session(turns=120)
    agent_id = await _agent(aops_pool)
    reader = ClaudeRecordReader(now_ns=now_ns, claude_home=tmp_path, session_id=session)
    position: str | None = None
    series = _Series()
    batch_size = 150
    for start in range(0, len(lines), batch_size):
        chunk = lines[start : start + batch_size]
        with target.open("a") as handle:
            _ = handle.write("\n".join(chunk) + "\n")
        before = reader.bytes_read
        batch = await reader.read_after("main", position)
        assert batch is not None
        series.read_bytes.append(reader.bytes_read - before)
        series.file_bytes.append(len("\n".join(chunk)) + 1)
        position = batch.position
        async with aops_pool.connection() as conn, conn.transaction():
            reads_before = await _database_reads(conn)
            count = await aappend_transcript_rows(
                conn, agent_id, session, [(r.record, r.ordinal, r.row) for r in batch.rows]
            )
            await aset_transcript_position(
                conn, agent_id, session, StoredPosition("main", position, -1), None
            )
            series.db_reads.append(await _database_reads(conn) - reads_before)
        series.written.append(count)
        series.stored_bytes.append(sum(len(json.dumps(r.row)) for r in batch.rows))
    await _assert_linear(aops_pool, agent_id, series, file_lines=len(lines))


async def test_positions_are_stored_per_record_and_upserted(aops_pool: AsyncConnectionPool) -> None:
    agent_id = await _agent(aops_pool)
    async with aops_pool.connection() as conn:
        empty = await aload_transcript_positions(conn, agent_id, "s1")
        await aset_transcript_position(
            conn, agent_id, "s1", StoredPosition("main", "p1", 4), "2.1.322"
        )
        await aset_transcript_positions(
            conn,
            agent_id,
            "s1",
            [StoredPosition("agent-a", "q1", -1), StoredPosition("main", "p2", 9)],
            None,
        )
        stored = await aload_transcript_positions(conn, agent_id, "s1")
        other = await aload_transcript_positions(conn, agent_id, "other-session")
        cursor = await conn.execute(
            "SELECT cli_version FROM agent_harness_transcript_positions "
            "WHERE agent_id = %s AND record = 'main'",
            (agent_id,),
        )
        version = await cursor.fetchone()
    assert empty == {} and other == {}
    assert stored == {
        "main": StoredPosition("main", "p2", 9),
        "agent-a": StoredPosition("agent-a", "q1", -1),
    }
    assert version == ("2.1.322",), (
        "a later position without a version keeps the one that was stored"
    )


async def test_a_batch_is_appended_in_one_go_and_calls_resolve_from_the_store_the_cache_and_the_batch(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id = await _agent(aops_pool)
    call = TimelineItem(item_id="x", kind="agent_code", payload="Task", tool_use_id="call-1")
    note = TimelineItem(item_id="x", kind="subagent_text", payload="hi")
    async with aops_pool.connection() as conn:
        first, learnt = await aappend_planned_timeline_items(
            conn,
            agent_id,
            "claude_code",
            [PlannedTimelineItem("k1", call, None)],
            calls={},
            projection_version=1,
        )
        assert [i.item_id for i in first] == ["h0.0"] and learnt == {"call-1": "h0.0"}
        written, learnt = await aappend_planned_timeline_items(
            conn,
            agent_id,
            "claude_code",
            [
                PlannedTimelineItem("k1", call, None),
                PlannedTimelineItem("k2", note, "call-1"),
                PlannedTimelineItem("k3", note, "call-gone"),
                PlannedTimelineItem("k4", note, "call-cached"),
            ],
            calls={"call-cached": "h99.0"},
            projection_version=1,
        )
    assert [(i.item_id, i.parent_item_id) for i in written] == [
        ("h1.0", "h0.0"),
        ("h2.0", None),
        ("h3.0", "h99.0"),
    ], "a key the agent has is skipped and the sequence stays contiguous"
    assert learnt == {"call-1": "h0.0", "call-gone": None}, "found and absent calls are learnt"
