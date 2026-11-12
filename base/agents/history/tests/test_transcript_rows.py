from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from psycopg_pool import AsyncConnectionPool

from base.agents.history.harness_messages import (
    StoredPosition,
    aappend_transcript_rows,
    adelete_expired_transcript_rows,
    aload_transcript_rows,
    aset_transcript_position,
)
from base.lm.harness.claude.transcript.reader import ClaudeRecordReader
from base.lm.harness.claude.transcript.tests.support import FIXTURES, rows

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


async def test_expired_rows_are_deleted_in_bounded_batches_oldest_first(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id = await _agent(aops_pool)
    async with aops_pool.connection() as conn:
        _ = await aappend_transcript_rows(
            conn, agent_id, SESSION, [("main", n, {"n": n}) for n in range(5)]
        )
        old = datetime.now(UTC) - timedelta(days=40)
        await conn.execute(
            "UPDATE agent_harness_transcript_rows SET written_at = %s WHERE ordinal < 4", (old,)
        )
        cutoff = datetime.now(UTC) - timedelta(days=30)
        assert await adelete_expired_transcript_rows(conn, cutoff=cutoff, limit=3) == 3
        assert await adelete_expired_transcript_rows(conn, cutoff=cutoff, limit=3) == 1
        assert await adelete_expired_transcript_rows(conn, cutoff=cutoff, limit=3) == 0
        left = await aload_transcript_rows(
            conn, agent_id, SESSION, "main", after_ordinal=-1, limit=10
        )
    assert left == [(4, {"n": 4})]


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
    return out


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
    reader = ClaudeRecordReader(claude_home=tmp_path, session_id=session)
    position: str | None = None
    written: list[int] = []
    stored_bytes: list[int] = []
    read_bytes: list[int] = []
    file_bytes: list[int] = []
    batch_size = 150
    for start in range(0, len(lines), batch_size):
        chunk = lines[start : start + batch_size]
        with target.open("a") as handle:
            _ = handle.write("\n".join(chunk) + "\n")
        before = reader.bytes_read
        batch = await reader.read_after("main", position)
        assert batch is not None
        read_bytes.append(reader.bytes_read - before)
        file_bytes.append(len("\n".join(chunk)) + 1)
        position = batch.position
        async with aops_pool.connection() as conn:
            count = await aappend_transcript_rows(
                conn, agent_id, session, [(r.record, r.ordinal, r.row) for r in batch.rows]
            )
            await aset_transcript_position(
                conn, agent_id, session, StoredPosition("main", position, -1), None
            )
        written.append(count)
        stored_bytes.append(sum(len(json.dumps(r.row)) for r in batch.rows))
    async with aops_pool.connection() as conn:
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
    assert total is not None and positions == (1,)
    assert int(total[0]) == sum(written) and sum(written) > 0
    batches = len(written)
    assert max(written) <= 1.2 * min(written[:-1] or written) + 5
    assert max(stored_bytes[:-1]) <= 1.2 * min(stored_bytes[:-1])
    mean_row_bytes = int(total[1]) / int(total[0])
    assert mean_row_bytes < 4 * (sum(stored_bytes) / sum(written))
    assert all(
        read <= 1.5 * size + 20_000 for read, size in zip(read_bytes, file_bytes, strict=True)
    )
    assert batches >= 6
