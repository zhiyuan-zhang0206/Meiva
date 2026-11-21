import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Literal, cast

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import Checkpoint, empty_checkpoint
from langgraph.checkpoint.postgres.base import BasePostgresSaver
from langgraph.constants import PUSH
from psycopg import AsyncConnection, errors
from psycopg.pq import PipelineStatus, TransactionStatus
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from agent.checkpoint import flush_checkpoint
from base.config import settings
from services.agent_runner.agent_host.daemon import build_checkpointer
from services.agent_runner.agent_host.pooled_checkpoint import (
    SELECT_COLUMNS,
    BlobCacheStats,
    PooledPostgresSaver,
)

_LOCK_KEY = 918273


@asynccontextmanager
async def _pool(size: int) -> AsyncGenerator[AsyncConnectionPool[AsyncConnection[DictRow]]]:
    async with AsyncConnectionPool[AsyncConnection[DictRow]](
        settings.data_plane.db_url,
        min_size=size,
        max_size=size,
        kwargs={"autocommit": True, "prepare_threshold": None, "row_factory": dict_row},
        open=False,
    ) as pool:
        await pool.wait()
        yield pool


# meiva:note
# A saver whose read of agent 101's thread takes a transaction-scoped advisory lock inside the
# query, so a test holding that lock leaves exactly that read blocked on the server.
class _ReadGatedSaver(PooledPostgresSaver):
    SELECT_SQL = PooledPostgresSaver.SELECT_SQL.replace(
        "select",
        f"select CASE WHEN thread_id = '101' THEN pg_advisory_xact_lock({_LOCK_KEY}) END AS test_gate,",
        1,
    )


def _config(thread_id: str, checkpoint_ns: str = "") -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns}}


def _checkpoint() -> Checkpoint:
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"value": ["persisted"]}
    checkpoint["channel_versions"] = {"value": "1"}
    return checkpoint


async def _wait_for_blocked_query(conn: AsyncConnection) -> None:
    async with asyncio.timeout(5):
        while True:
            cur = await conn.execute(
                "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a USING (pid) "
                "WHERE NOT l.granted AND a.datname = current_database()"
            )
            row = await cur.fetchone()
            assert row is not None
            if row[0]:
                return
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("operation", ["read", "write"])
async def test_blocked_agent_checkpoint_does_not_block_another_agent(
    adb_conn: AsyncConnection,
    operation: Literal["read", "write"],
) -> None:
    async with _pool(2) as pool:
        saver = (_ReadGatedSaver if operation == "read" else PooledPostgresSaver)(pool)
        checkpoint = _checkpoint()
        blocked_config = await saver.aput(
            _config("101"), checkpoint, {"source": "input", "step": -1}, {"value": "1"}
        )
        await saver.aput(
            _config("202"), _checkpoint(), {"source": "input", "step": -1}, {"value": "1"}
        )

        if operation == "read":
            await adb_conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
        else:
            await adb_conn.execute(
                "SELECT 1 FROM checkpoints WHERE thread_id = %s FOR UPDATE",
                ("101",),
            )

        async def run_blocked_operation() -> None:
            if operation == "read":
                await saver.aget_tuple(blocked_config)
            else:
                await saver.aput(
                    blocked_config,
                    checkpoint,
                    {"source": "update", "step": 0},
                    {"value": "1"},
                )

        blocked = asyncio.create_task(run_blocked_operation())
        try:
            await _wait_for_blocked_query(adb_conn)
            async with asyncio.timeout(2):
                free_config = await saver.aput(
                    _config("202"),
                    _checkpoint(),
                    {"source": "update", "step": 0},
                    {"value": "1"},
                )
                await saver.aput_writes(free_config, [(PUSH, "value")], "free-task")
                loaded = await saver.aget_tuple(free_config)
            assert loaded is not None
            assert loaded.checkpoint["channel_values"]["value"] == ["persisted"]
            assert loaded.pending_writes == [("free-task", PUSH, "value")]
            assert not blocked.done()
        finally:
            await adb_conn.rollback()
            await asyncio.wait_for(blocked, timeout=5)


async def test_same_agent_flush_serializes_with_newer_checkpoint(adb_conn: AsyncConnection) -> None:
    async with _pool(2) as pool:
        saver = await build_checkpointer(
            cast(AsyncConnectionPool[AsyncConnection], pool), checkpoint_interval=4
        )
        initial = await saver.aput(
            _config("101"), _checkpoint(), {"source": "input", "step": -1}, {"value": "1"}
        )
        tail, newer = _checkpoint(), _checkpoint()
        await saver.aput(initial, tail, {"source": "loop", "step": 1}, {"value": "1"})
        tail_config = await PooledPostgresSaver(pool).aput(
            initial, tail, {"source": "loop", "step": 1}, {"value": "1"}
        )
        await adb_conn.execute(
            "SELECT 1 FROM checkpoints WHERE thread_id = %s AND checkpoint_id = %s FOR UPDATE",
            ("101", tail["id"]),
        )
        flushing = asyncio.create_task(flush_checkpoint(saver, 101))
        started = asyncio.Event()

        async def write_newer() -> None:
            started.set()
            await saver.aput(tail_config, newer, {"source": "loop", "step": 2}, {"value": "1"})

        writing: asyncio.Task[None] | None = None
        try:
            await _wait_for_blocked_query(adb_conn)
            writing = asyncio.create_task(write_newer())
            await asyncio.wait_for(started.wait(), timeout=2)
            assert not writing.done()
            assert not flushing.done()
        finally:
            await adb_conn.rollback()
            await asyncio.wait_for(flushing, timeout=5)
            if writing is not None:
                await asyncio.wait_for(writing, timeout=5)

        await flush_checkpoint(saver, 101)
        stored = await saver.aget_tuple(_config("101"))
        assert stored is not None
        assert stored.checkpoint["id"] == newer["id"]
        assert stored.parent_config == tail_config
        stored_tail = await saver.aget_tuple(tail_config)
        assert stored_tail is not None
        assert stored_tail.parent_config == initial


@pytest.mark.parametrize("supports_pipeline", [True, False], ids=["pipeline", "transaction"])
@pytest.mark.parametrize("failure", ["error", "cancel"])
async def test_failed_cursor_returns_a_clean_reusable_pool_connection(
    adb_conn: AsyncConnection,
    supports_pipeline: bool,
    failure: Literal["error", "cancel"],
) -> None:
    async with _pool(1) as pool:
        saver = PooledPostgresSaver(pool)
        async with pool.connection() as conn:
            original_backend = conn.info.backend_pid

        async def fail() -> None:
            async with saver._cursor(pipeline=supports_pipeline) as cur:
                if failure == "error":
                    await cur.execute("SELECT 1 / 0")
                else:
                    await cur.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
                await cur.fetchone()

        if failure == "error":
            with pytest.raises(errors.DivisionByZero):
                await fail()
        else:
            await adb_conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
            pending = asyncio.create_task(fail())
            try:
                await _wait_for_blocked_query(adb_conn)
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(pending, timeout=5)
            finally:
                await adb_conn.rollback()
                if not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)

        async with pool.connection() as conn:
            assert conn.info.backend_pid == original_backend
            assert conn.info.transaction_status == TransactionStatus.IDLE
            assert conn.info.pipeline_status == PipelineStatus.OFF
        saved = await saver.aput(
            _config("after-failure"),
            _checkpoint(),
            {"source": "input", "step": -1},
            {"value": "1"},
        )
        loaded = await saver.aget_tuple(saved)
        assert loaded is not None
        assert loaded.checkpoint["channel_values"]["value"] == ["persisted"]


async def test_blob_cache_skips_unchanged_blobs_on_second_read(
    adb_conn: AsyncConnection,
) -> None:
    from langgraph.checkpoint.serde.types import _DeltaSnapshot

    async with _pool(2) as pool:
        saver = PooledPostgresSaver(pool, blob_cache_max_bytes=1024 * 1024)
        cp1 = empty_checkpoint()
        cp1["channel_values"] = {
            "messages": _DeltaSnapshot(["m1", "m2", "m3"]),
            "memory": {"notes": "x" * 2000},
            "step_tag": "v1",
        }
        cp1["channel_versions"] = {"messages": "1", "memory": "1", "step_tag": "1"}
        cfg1 = await saver.aput(
            _config("blob-cache-t1"),
            cp1,
            {"source": "input", "step": 0},
            {"messages": "1", "memory": "1", "step_tag": "1"},
        )

        first = await saver.aget_tuple(cfg1)
        assert first is not None
        assert first.checkpoint["channel_values"]["messages"].value == ["m1", "m2", "m3"]
        after_first = saver.blob_cache_stats()
        assert after_first.fetched_blobs == 2
        assert after_first.hits == 0
        assert after_first.total_bytes > 2000

        second = await saver.aget_tuple(_config("blob-cache-t1"))
        assert second is not None
        after_second = saver.blob_cache_stats()
        assert after_second.fetched_blobs == 2
        assert after_second.hits == 2
        assert second.checkpoint["channel_values"]["messages"].value == ["m1", "m2", "m3"]
        assert second.checkpoint["channel_values"]["memory"] == {"notes": "x" * 2000}


async def test_blob_cache_fetches_updated_channel_and_drops_on_delete(
    adb_conn: AsyncConnection,
) -> None:
    from langgraph.checkpoint.serde.types import _DeltaSnapshot

    async with _pool(2) as pool:
        saver = PooledPostgresSaver(pool, blob_cache_max_bytes=1024 * 1024)
        cp1 = empty_checkpoint()
        cp1["channel_values"] = {
            "messages": _DeltaSnapshot(["m1", "m2"]),
            "memory": {"notes": "v1"},
        }
        cp1["channel_versions"] = {"messages": "1", "memory": "1"}
        cfg1 = await saver.aput(
            _config("blob-cache-t2"),
            cp1,
            {"source": "input", "step": 0},
            {"messages": "1", "memory": "1"},
        )
        assert await saver.aget_tuple(cfg1) is not None

        cp2 = empty_checkpoint()
        cp2["channel_values"] = {
            "messages": _DeltaSnapshot(["m1", "m2"]),
            "memory": {"notes": "v2"},
        }
        cp2["channel_versions"] = {"messages": "1", "memory": "2"}
        await saver.aput(cfg1, cp2, {"source": "loop", "step": 1}, {"memory": "2"})

        updated = await saver.aget_tuple(_config("blob-cache-t2"))
        assert updated is not None
        stats = saver.blob_cache_stats()
        assert stats.fetched_blobs == 3
        assert stats.hits == 1
        assert updated.checkpoint["channel_values"]["messages"].value == ["m1", "m2"]
        assert updated.checkpoint["channel_values"]["memory"] == {"notes": "v2"}

        await saver.adelete_thread("blob-cache-t2")
        assert saver.blob_cache_stats().total_bytes == 0
        assert await saver.aget_tuple(_config("blob-cache-t2")) is None
        assert await saver.aget_tuple(cfg1) is None


async def test_blob_cache_eviction_and_disabled(adb_conn: AsyncConnection) -> None:
    async with _pool(2) as pool:
        cap = 700
        tiny = PooledPostgresSaver(pool, blob_cache_max_bytes=cap)
        cp3 = empty_checkpoint()
        expected = {
            "a": {"v": "x" * 300},
            "b": {"v": "y" * 300},
            "c": {"v": "z" * 300},
        }
        cp3["channel_values"] = expected
        cp3["channel_versions"] = {"a": "1", "b": "1", "c": "1"}
        await tiny.aput(
            _config("blob-cache-evict"),
            cp3,
            {"source": "input", "step": 0},
            {"a": "1", "b": "1", "c": "1"},
        )
        first = await tiny.aget_tuple(_config("blob-cache-evict"))
        assert first is not None
        assert first.checkpoint["channel_values"] == expected
        stats1 = tiny.blob_cache_stats()
        assert stats1.fetched_blobs == 3
        assert stats1.hits == 0
        assert 300 < stats1.total_bytes <= cap

        second = await tiny.aget_tuple(_config("blob-cache-evict"))
        assert second is not None
        assert second.checkpoint["channel_values"] == expected
        stats2 = tiny.blob_cache_stats()
        assert stats2.fetched_blobs == 4
        assert stats2.hits == 2
        assert 300 < stats2.total_bytes <= cap

        disabled = PooledPostgresSaver(pool, blob_cache_max_bytes=0)
        off_read = await disabled.aget_tuple(_config("blob-cache-evict"))
        assert off_read is not None
        assert off_read.checkpoint["channel_values"] == expected
        assert disabled.blob_cache_stats() == BlobCacheStats(hits=0, fetched_blobs=0, total_bytes=0)


async def test_blob_cache_isolates_namespaces_with_same_version(
    adb_conn: AsyncConnection,
) -> None:
    async with _pool(2) as pool:
        saver = PooledPostgresSaver(pool, blob_cache_max_bytes=1024 * 1024)
        root_cp = empty_checkpoint()
        root_cp["channel_values"] = {"payload": {"from": "root"}}
        root_cp["channel_versions"] = {"payload": "1"}
        await saver.aput(
            _config("blob-cache-ns", ""),
            root_cp,
            {"source": "input", "step": 0},
            {"payload": "1"},
        )

        sub_cp = empty_checkpoint()
        sub_cp["channel_values"] = {"payload": {"from": "sub"}}
        sub_cp["channel_versions"] = {"payload": "1"}
        await saver.aput(
            _config("blob-cache-ns", "sub:1"),
            sub_cp,
            {"source": "input", "step": 0},
            {"payload": "1"},
        )

        root_first = await saver.aget_tuple(_config("blob-cache-ns", ""))
        assert root_first is not None
        assert root_first.checkpoint["channel_values"]["payload"] == {"from": "root"}

        sub_first = await saver.aget_tuple(_config("blob-cache-ns", "sub:1"))
        assert sub_first is not None
        assert sub_first.checkpoint["channel_values"]["payload"] == {"from": "sub"}
        assert saver.blob_cache_stats().fetched_blobs == 2
        assert saver.blob_cache_stats().hits == 0

        root_second = await saver.aget_tuple(_config("blob-cache-ns", ""))
        sub_second = await saver.aget_tuple(_config("blob-cache-ns", "sub:1"))
        assert root_second is not None
        assert sub_second is not None
        assert root_second.checkpoint["channel_values"]["payload"] == {"from": "root"}
        assert sub_second.checkpoint["channel_values"]["payload"] == {"from": "sub"}
        assert saver.blob_cache_stats().fetched_blobs == 2
        assert saver.blob_cache_stats().hits == 2


async def test_blob_cache_historical_checkpoint_id_reads_old_value(
    adb_conn: AsyncConnection,
) -> None:
    async with _pool(2) as pool:
        saver = PooledPostgresSaver(pool, blob_cache_max_bytes=1024 * 1024)
        cp1 = empty_checkpoint()
        cp1["channel_values"] = {"payload": {"gen": 1}}
        cp1["channel_versions"] = {"payload": "1"}
        cfg1 = await saver.aput(
            _config("blob-cache-hist"),
            cp1,
            {"source": "input", "step": 0},
            {"payload": "1"},
        )

        cp2 = empty_checkpoint()
        cp2["channel_values"] = {"payload": {"gen": 2}}
        cp2["channel_versions"] = {"payload": "2"}
        await saver.aput(
            cfg1,
            cp2,
            {"source": "loop", "step": 1},
            {"payload": "2"},
        )

        latest_first = await saver.aget_tuple(_config("blob-cache-hist"))
        assert latest_first is not None
        assert latest_first.checkpoint["channel_values"]["payload"] == {"gen": 2}

        historical = await saver.aget_tuple(cfg1)
        assert historical is not None
        assert historical.checkpoint["channel_values"]["payload"] == {"gen": 1}

        latest_second = await saver.aget_tuple(_config("blob-cache-hist"))
        assert latest_second is not None
        assert latest_second.checkpoint["channel_values"]["payload"] == {"gen": 2}


def test_select_columns_matches_upstream_sql() -> None:
    assert SELECT_COLUMNS == BasePostgresSaver.SELECT_SQL
