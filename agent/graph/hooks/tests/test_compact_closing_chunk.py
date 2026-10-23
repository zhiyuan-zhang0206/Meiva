"""Compaction stamps its boundary, then enqueues the closing understanding chunk."""

from __future__ import annotations

from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage
from psycopg_pool import AsyncConnectionPool

from agent.graph.hooks import compact
from agent.state import AgentState
from agent.state_channels import CompactState
from base.log.tests.log_records import LogRecords

_AGENT = 9


def _request(n: int) -> list[AnyMessage]:
    return [
        SystemMessage(content="head", id="h"),
        *(HumanMessage(content=str(i), id=f"m{i}") for i in range(1, n)),
    ]


async def _checkpoint(pool: AsyncConnectionPool, checkpoint_id: str) -> None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, checkpoint,"
            " metadata) VALUES (%s, '', %s, 'x', '{}'::jsonb, '{}'::jsonb)",
            (str(_AGENT), checkpoint_id),
        )


async def _jobs(pool: AsyncConnectionPool) -> list[tuple[int, int, int, int, str, str | None]]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT agent_id, compact_version, start_index, end_index, end_msg_id,"
            " boundary_checkpoint_id FROM understanding_chunk_jobs ORDER BY id"
        )
        return await cur.fetchall()


async def _stamped(pool: AsyncConnectionPool, checkpoint_id: str) -> bool:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT metadata ? 'compact_boundary' FROM checkpoints"
            " WHERE thread_id = %s AND checkpoint_id = %s",
            (str(_AGENT), checkpoint_id),
        )
        row = await cur.fetchone()
    assert row is not None
    return bool(row[0])


async def test_stamp_enqueues_the_closing_chunk_against_the_stamped_checkpoint(
    db_url: str, aops_pool: AsyncConnectionPool
) -> None:
    await _checkpoint(aops_pool, "cp-6")
    await _checkpoint(aops_pool, "cp-7")
    state = AgentState(
        messages=_request(8), compact=CompactState(version=1, understanding_cut_index=3)
    )

    await compact.stamp_compact_boundary(aops_pool, _AGENT, state, understanding_enabled=True)

    assert await _jobs(aops_pool) == [(_AGENT, 1, 3, 8, "m7", "cp-7")]
    assert await _stamped(aops_pool, "cp-7")
    assert not await _stamped(aops_pool, "cp-6")


async def test_a_thread_without_a_checkpoint_stamps_nothing_and_enqueues_no_closing_chunk(
    db_url: str, aops_pool: AsyncConnectionPool
) -> None:
    await compact.stamp_compact_boundary(
        aops_pool, _AGENT, AgentState(messages=_request(8)), understanding_enabled=True
    )

    assert await _jobs(aops_pool) == []


async def test_a_stamp_that_fails_is_reported_and_enqueues_no_closing_chunk(
    db_url: str, loguru_records: LogRecords
) -> None:
    unopened: AsyncConnectionPool = AsyncConnectionPool(db_url, open=False)

    await compact.stamp_compact_boundary(
        unopened, _AGENT, AgentState(messages=_request(8)), understanding_enabled=True
    )

    assert any("compact boundary stamp failed" in record["message"] for record in loguru_records)


async def test_with_understanding_off_the_boundary_is_stamped_and_no_chunk_is_enqueued(
    db_url: str, aops_pool: AsyncConnectionPool
) -> None:
    await _checkpoint(aops_pool, "cp-7")
    state = AgentState(
        messages=_request(8), compact=CompactState(version=1, understanding_cut_index=3)
    )

    await compact.stamp_compact_boundary(aops_pool, _AGENT, state, understanding_enabled=False)

    assert await _jobs(aops_pool) == []
    assert await _stamped(aops_pool, "cp-7")


async def test_without_a_state_the_boundary_is_stamped_and_nothing_is_enqueued(
    db_url: str, aops_pool: AsyncConnectionPool
) -> None:
    await _checkpoint(aops_pool, "cp-7")

    await compact.stamp_compact_boundary(aops_pool, _AGENT, None, understanding_enabled=True)

    assert await _jobs(aops_pool) == []
    assert await _stamped(aops_pool, "cp-7")
