# meiva:note
# Single-segment chunk location against real PostgreSQL checkpoints: bit-for-bit parity with
# full-history stitching across empty, uncompacted and multi-segment threads, lazy
# `compact_segment_shape` stamping and malformed-metadata repair, and zero reconstruction of
# stamped older boundaries.
# meiva:link base/agents/history/docs/range-read-contracts.meiva.okf.md
# meiva:link base/agents/history/hierarchy/docs/chunks.meiva.okf.md

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast

import psycopg
import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.base import CheckpointMetadata, empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from base.agents.history.checkpoint import (
    list_compact_boundary_checkpoint_ids,
    load_checkpoint_history_full,
)
from base.agents.history.checkpoint_segment import (
    SEGMENT_SHAPE_METADATA_KEY,
    BoundaryCheckpointGoneError,
)
from base.agents.history.hierarchy.chunks import (
    Chunk,
    ChunkDriftError,
    ChunkEmptyError,
    ChunkJob,
    ChunkNotReadyError,
    LocatedChunk,
    enqueue_chunk,
    locate_chunk,
)
from base.clock.tests.fakes import system_clock
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.protocols import DbConfig
from base.lm.catalog import ModelCatalog
from base.telemetry.tests.capture.event_capture import EmittedEvents
from services.agent_runner.understanding import chunk_consumer as loop
from services.agent_runner.understanding.chunk_locator import locate_job_chunk
from services.agent_runner.understanding.tests.chunk_faults import ChunkSeams
from services.agent_runner.understanding.tests.chunk_faults import chunk_seams as chunk_seams
from services.agent_runner.understanding.tests.chunk_faults import (
    chunk_timestamps as chunk_timestamps,
)
from services.agent_runner.understanding.tests.consumer_helpers import (
    consumer_config,
    lm_wiring,
)
from services.agent_runner.understanding.tests.consumer_history import (
    job_statuses,
    written_nodes,
)

CLOCK = system_clock()


def _database(db_url: str, database_gate: ProcessDbGate) -> Database:
    return Database(
        DbConfig(
            db_url=db_url,
            db_sslmode="disable",
            db_pool_min_size=1,
            db_pool_max_size=2,
            pgbouncer_enabled=False,
            db_authority_refusal=None,
        ),
        gate=database_gate,
    )


def _put_checkpoint(
    db_url: str,
    agent_id: int,
    messages: Sequence[BaseMessage],
    *,
    step: int,
    compact_boundary: bool = False,
) -> str:
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"messages": list(messages)}
    checkpoint["channel_versions"] = {"messages": str(step), "__start__": "1"}
    metadata: dict[str, object] = {"source": "loop", "step": step, "parents": {}}
    if compact_boundary:
        metadata["compact_boundary"] = True
    with PostgresSaver.from_conn_string(db_url) as saver:
        saver.setup()
        saved = saver.put(
            config={"configurable": {"thread_id": str(agent_id), "checkpoint_ns": ""}},
            checkpoint=checkpoint,
            metadata=cast(CheckpointMetadata, metadata),
            new_versions={"messages": str(step)},
        )
    configurable = saved.get("configurable") or {}
    return str(configurable["checkpoint_id"])


def _job(
    agent_id: int,
    start_index: int,
    end_index: int,
    end_msg_id: str,
    *,
    boundary_checkpoint_id: str | None = None,
) -> ChunkJob:
    return ChunkJob(
        id=1,
        agent_id=agent_id,
        compact_version=0,
        start_index=start_index,
        end_index=end_index,
        end_msg_id=end_msg_id,
        boundary_checkpoint_id=boundary_checkpoint_id,
        attempts=1,
        age_seconds=0.0,
    )


def _oracle_locate(db: Database, job: ChunkJob) -> LocatedChunk:
    history = load_checkpoint_history_full(db, job.agent_id)
    closing_segment: int | None = None
    if job.boundary_checkpoint_id is not None:
        ascending = list(reversed(list_compact_boundary_checkpoint_ids(db, job.agent_id)))
        closing_segment = ascending.index(job.boundary_checkpoint_id)
    return locate_chunk(
        history,
        start_index=job.start_index,
        end_index=job.end_index,
        end_msg_id=job.end_msg_id,
        closing_segment=closing_segment,
    )


def _assert_matches_oracle(db: Database, job: ChunkJob) -> None:
    try:
        expected = _oracle_locate(db, job)
    except Exception as expected_exc:
        with pytest.raises(type(expected_exc)) as actual_exc:
            locate_job_chunk(db, job)
        if not isinstance(expected_exc, ValueError):
            assert str(actual_exc.value) == str(expected_exc)
        return
    assert locate_job_chunk(db, job) == expected


def _msg(msg_id: str, kind: str = "h") -> BaseMessage:
    kwargs = {"meiva_created_at": datetime(2026, 10, 5, 0, 0, tzinfo=UTC).isoformat()}
    if kind == "ai":
        return AIMessage(content=f"reply {msg_id}", id=msg_id, additional_kwargs=kwargs)
    return HumanMessage(content=f"turn {msg_id}", id=msg_id, additional_kwargs=kwargs)


def test_empty_and_single_segment_histories_match_full_history_oracle(
    db_url: str, database_gate: ProcessDbGate
) -> None:
    db = _database(db_url, database_gate)
    with PostgresSaver.from_conn_string(db_url) as saver:
        saver.setup()

    _assert_matches_oracle(db, _job(101, 1, 3, "m1"))
    with pytest.raises(BoundaryCheckpointGoneError):
        locate_job_chunk(db, _job(101, 1, 3, "m1", boundary_checkpoint_id="cp-missing"))

    _put_checkpoint(db_url, 102, [], step=1)
    _assert_matches_oracle(db, _job(102, 0, 1, "m0"))

    _put_checkpoint(db_url, 103, [SystemMessage(content="sys", id="sys")], step=1)
    _assert_matches_oracle(db, _job(103, 1, 2, "m0"))

    _put_checkpoint(
        db_url,
        104,
        [SystemMessage(content="sys", id="sys"), _msg("m0"), _msg("m1", "ai"), _msg("m2")],
        step=1,
    )
    _assert_matches_oracle(db, _job(104, 1, 3, "m1"))
    _assert_matches_oracle(db, _job(104, 1, 6, "m4"))
    _assert_matches_oracle(db, _job(104, 1, 3, "wrong"))
    _assert_matches_oracle(db, _job(104, 3, 3, "m1"))

    _put_checkpoint(db_url, 105, [_msg("h0"), _msg("h1", "ai")], step=1)
    _assert_matches_oracle(db, _job(105, 0, 2, "h1"))
    _assert_matches_oracle(db, _job(105, 0, 4, "h3"))


def test_multi_segment_live_and_closing_chunks_match_oracle_across_latest_is_boundary(
    db_url: str, database_gate: ProcessDbGate
) -> None:
    db = _database(db_url, database_gate)
    b0 = _put_checkpoint(db_url, 201, [], step=1, compact_boundary=True)
    b1 = _put_checkpoint(
        db_url,
        201,
        [_msg("m0"), _msg("m1", "ai"), _msg("m2")],
        step=2,
        compact_boundary=True,
    )
    b2 = _put_checkpoint(
        db_url,
        201,
        [SystemMessage(content="sys", id="sys-2"), _msg("m3"), _msg("m4", "ai"), _msg("m5")],
        step=3,
        compact_boundary=True,
    )

    # meiva:note
    # When `latest` is itself `b2` (`latest_is_boundary` True), segment count stays 3 rather than 4.
    _assert_matches_oracle(db, _job(201, 1, 4, "m5"))
    _assert_matches_oracle(db, _job(201, 1, 6, "m9"))
    _assert_matches_oracle(db, _job(201, 1, 3, "wrong"))
    _assert_matches_oracle(db, _job(201, 0, 2, "m1"))

    # meiva:note
    # Append open segment 3 after compaction (`latest_is_boundary` False).
    _put_checkpoint(
        db_url,
        201,
        [SystemMessage(content="sys", id="sys-3"), _msg("sum"), _msg("m6", "ai")],
        step=4,
        compact_boundary=False,
    )
    for candidate in (
        _job(201, 1, 3, "m6"),
        _job(201, 1, 4, "m5"),
        _job(201, 0, 3, "m2"),
        _job(201, 1, 4, "not-yet-flushed"),
        _job(201, 1, 2, "drifted"),
        _job(201, 0, 1, "m0", boundary_checkpoint_id=b0),
        _job(201, 0, 2, "m1", boundary_checkpoint_id=b1),
        _job(201, 1, 3, "m4", boundary_checkpoint_id=b2),
        _job(201, 2, 7, "m9", boundary_checkpoint_id=b2),
        _job(201, 5, 7, "m9", boundary_checkpoint_id=b2),
        _job(201, 1, 3, "wrong", boundary_checkpoint_id=b2),
    ):
        _assert_matches_oracle(db, candidate)
    with pytest.raises(ChunkEmptyError, match="lacks request indices 4 to 7"):
        locate_job_chunk(db, _job(201, 5, 7, "m9", boundary_checkpoint_id=b2))


def test_stamped_compact_segment_shape_skips_reconstructing_prior_boundaries(
    db_url: str, database_gate: ProcessDbGate
) -> None:
    db = _database(db_url, database_gate)
    b0 = _put_checkpoint(
        db_url,
        301,
        [
            SystemMessage(content="sys", id="sys-0"),
            _msg("s0-0"),
            _msg("s0-1", "ai"),
            _msg("s0-2"),
            _msg("s0-3", "ai"),
        ],
        step=1,
        compact_boundary=True,
    )
    b1 = _put_checkpoint(
        db_url,
        301,
        [
            SystemMessage(content="sys", id="sys-1"),
            _msg("s1-0"),
            _msg("s1-1", "ai"),
            _msg("s1-2"),
            _msg("s1-3", "ai"),
        ],
        step=2,
        compact_boundary=True,
    )
    b2 = _put_checkpoint(
        db_url,
        301,
        [SystemMessage(content="sys", id="sys-2"), _msg("s2-0"), _msg("s2-1", "ai")],
        step=3,
        compact_boundary=True,
    )
    latest_id = _put_checkpoint(
        db_url,
        301,
        [SystemMessage(content="sys", id="sys-3"), _msg("s3-0"), _msg("s3-1", "ai")],
        step=4,
        compact_boundary=False,
    )

    first_located = locate_job_chunk(db, _job(301, 1, 3, "s3-1"))
    assert first_located.span == (11, 12)

    with psycopg.connect(db_url) as conn:
        rows = conn.execute(
            "SELECT checkpoint_id, metadata ? %s FROM checkpoints"
            " WHERE thread_id = '301' ORDER BY checkpoint_id ASC",
            (SEGMENT_SHAPE_METADATA_KEY,),
        ).fetchall()
        assert rows == [(b0, True), (b1, True), (b2, True), (latest_id, False)]
        conn.execute(
            "DELETE FROM checkpoint_blobs WHERE thread_id = '301' AND version IN ('1', '2')"
        )
        conn.execute(
            "UPDATE checkpoints SET checkpoint = '{}'::jsonb"
            " WHERE thread_id = '301' AND checkpoint_id IN (%s, %s)",
            (b0, b1),
        )
        conn.commit()

    # meiva:note
    # Wiping `b0` and `b1` blobs breaks any full-history read; single-segment location resolves
    # live hits, `not_ready`, `drift` and `b2` closing chunks from stamped boundary shapes alone.
    assert locate_job_chunk(db, _job(301, 1, 3, "s3-1")).span == (11, 12)
    with pytest.raises(ChunkNotReadyError):
        locate_job_chunk(db, _job(301, 1, 4, "s3-pending"))
    with pytest.raises(ChunkDriftError):
        locate_job_chunk(db, _job(301, 1, 3, "drifted-id"))
    closing_b2 = locate_job_chunk(db, _job(301, 1, 3, "s2-1", boundary_checkpoint_id=b2))
    assert closing_b2.span == (9, 10)
    assert [m.id for m in closing_b2.messages] == ["s2-0", "s2-1"]


def test_malformed_compact_segment_shape_metadata_is_repaired_from_checkpoint(
    db_url: str, database_gate: ProcessDbGate
) -> None:
    db = _database(db_url, database_gate)
    b0 = _put_checkpoint(
        db_url,
        401,
        [SystemMessage(content="sys", id="sys"), _msg("m0"), _msg("m1", "ai")],
        step=1,
        compact_boundary=True,
    )
    b1 = _put_checkpoint(
        db_url,
        401,
        [SystemMessage(content="sys", id="sys"), _msg("m2"), _msg("m3", "ai")],
        step=2,
        compact_boundary=True,
    )
    _put_checkpoint(
        db_url,
        401,
        [SystemMessage(content="sys", id="sys"), _msg("m4"), _msg("m5", "ai")],
        step=3,
    )

    bad_shapes: tuple[object, ...] = (
        "not-an-object",
        {"v": 99, "head": 1, "body": 2, "ids": ["m0", "m1"]},
        {"v": 1, "head": 1, "body": 5, "ids": ["m0"]},
        {"v": 1, "head": 1, "body": "99999999999999999999", "ids": []},
    )
    for bad_shape in bad_shapes:
        with psycopg.connect(db_url) as conn:
            conn.execute(
                "UPDATE checkpoints"
                " SET metadata = jsonb_set(metadata, '{compact_segment_shape}', %s::jsonb, true)"
                " WHERE thread_id = '401' AND checkpoint_id IN (%s, %s)",
                (Jsonb(bad_shape), b0, b1),
            )
            conn.commit()
        located = locate_job_chunk(db, _job(401, 1, 3, "m5"))
        assert located.span == (5, 6)
        with psycopg.connect(db_url) as conn:
            repaired = conn.execute(
                "SELECT metadata->%s FROM checkpoints WHERE thread_id = '401' AND checkpoint_id = %s",
                (SEGMENT_SHAPE_METADATA_KEY, b0),
            ).fetchone()
        assert repaired == ({"v": 1, "head": 1, "body": 2, "ids": ["m0", "m1"]},)


async def test_consumer_end_to_end_uses_default_single_segment_locator(
    db_url: str,
    database_gate: ProcessDbGate,
    aops_pool: AsyncConnectionPool,
    model_catalog: ModelCatalog,
    chunk_seams: ChunkSeams,
    emitted_events: EmittedEvents,
) -> None:
    db = _database(db_url, database_gate)
    b0 = _put_checkpoint(
        db_url,
        501,
        [SystemMessage(content="sys", id="sys"), _msg("m0"), _msg("m1", "ai")],
        step=1,
        compact_boundary=True,
    )
    _put_checkpoint(
        db_url,
        501,
        [SystemMessage(content="sys", id="sys"), _msg("m2"), _msg("m3", "ai")],
        step=2,
    )
    await enqueue_chunk(
        aops_pool,
        501,
        compact_version=0,
        chunk=Chunk(1, 3),
        end_msg_id="m1",
        boundary_checkpoint_id=b0,
    )
    await enqueue_chunk(
        aops_pool,
        501,
        compact_version=1,
        chunk=Chunk(1, 3),
        end_msg_id="m3",
    )
    await enqueue_chunk(
        aops_pool,
        501,
        compact_version=2,
        chunk=Chunk(1, 3),
        end_msg_id="m3",
        boundary_checkpoint_id="cp-gone",
    )
    await loop.Consumer(
        aops_pool,
        db,
        [],
        clock=CLOCK,
        **lm_wiring(model_catalog),
        config=consumer_config(),
        describe=chunk_seams.describe,
    ).run_until_idle()

    assert await written_nodes(aops_pool) == [
        (501, 1, 1, 2, "what happened"),
        (501, 1, 3, 4, "what happened"),
    ]
    assert await job_statuses(aops_pool) == [
        ("done", 1, None),
        ("done", 1, None),
        ("failed", 1, "boundary checkpoint cp-gone is gone"),
    ]
    assert "understanding_chunk_failed" in emitted_events.names()


def test_external_harness_chunk_location(database: Database) -> None:
    from base.agents.history.timeline_item import TimelineItem

    harness_agent = 8888
    t0 = datetime(2026, 10, 5, tzinfo=UTC)
    items = [
        TimelineItem(
            item_id="h0.0",
            kind="inbound_chat",
            payload="hello from user",
            created_at=t0.isoformat(),
        ),
        TimelineItem(
            item_id="h1.0",
            kind="agent_reasoning",
            payload="thinking about the task",
            created_at=t0.isoformat(),
            reasoning_ms=1000,
        ),
        TimelineItem(
            item_id="h2.0",
            kind="agent_code",
            payload="ls",
            created_at=t0.isoformat(),
            code_elapsed_ms=500,
        ),
        TimelineItem(
            item_id="h3.0",
            kind="code_output",
            payload="output.txt",
            created_at=t0.isoformat(),
            exec_ms=200,
        ),
        TimelineItem(
            item_id="h4.0",
            kind="agent_chat",
            payload="finished",
            created_at=t0.isoformat(),
        ),
    ]
    with database.connect(autocommit=True) as conn:
        conn.execute(
            "INSERT INTO agents (id, label, created_at) VALUES (%s, 'test-external-chunk', now())"
            " ON CONFLICT (id) DO NOTHING",
            (harness_agent,),
        )
        conn.execute(
            "INSERT INTO agents_meta (id, harness, status) VALUES (%s, 'claude_code', 'running')"
            " ON CONFLICT (id) DO NOTHING",
            (harness_agent,),
        )
        for seq, item in enumerate(items):
            conn.execute(
                "INSERT INTO agent_harness_messages (agent_id, seq, harness, item, created_at)"
                " VALUES (%s, %s, 'claude_code', %s::jsonb, %s::timestamptz)",
                (harness_agent, seq, item.model_dump_json(), item.created_at),
            )
    job = ChunkJob(1, harness_agent, 0, 0, 3, "h2.0", None, 1)
    located = locate_job_chunk(database, job)
    assert len(located.messages) == 3
    assert located.messages[-1].id == "h2.0"
