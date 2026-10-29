# meiva:note
# These existing fault tests use heterogeneous spy state at precise failure boundaries.
# pyright: reportUnknownMemberType = warning
# pyright: reportUnknownArgumentType = warning
# pyright: reportUnknownLambdaType = warning
# pyright: reportUnknownVariableType = warning
# meiva:note
# A claimed chunk job's wait and the span it ends up describing: the spacing a released job keeps,
# the give-up clock, cancellation, the part of the chunk existing nodes already cover, and a
# closing chunk past its snapshot.
from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock

import pytest
from psycopg_pool import AsyncConnectionPool

from base.agents.history.hierarchy.chunk_generate import ChunkResult
from base.agents.history.hierarchy.chunks import (
    Chunk,
    LocatedChunk,
    enqueue_chunk,
)
from base.agents.messages.timestamp_policy import TimestampPolicy
from base.clock.tests.fakes import system_clock
from base.lm.catalog import ModelCatalog
from base.telemetry.tests.capture.event_capture import EmittedEvents
from services.agent_runner.understanding import chunk_consumer as loop
from services.agent_runner.understanding.tests.chunk_faults import ChunkSeams as ChunkSeams
from services.agent_runner.understanding.tests.chunk_faults import chunk_result, run_rounds
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

# meiva:note
# One clock for every consumer in this file: the value a host builds once at its entry and hands
# to each loop, so the tests pass what production passes.
CLOCK = system_clock()


async def test_a_released_job_waits_its_spacing_from_the_release_not_from_the_claim(
    aops_pool: AsyncConnectionPool,
) -> None:
    from base.agents.history.hierarchy.chunks import claim_job, release_job

    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    job = await claim_job(aops_pool)
    assert job is not None
    # meiva:note
    # a slow attempt: claimed long ago
    async with aops_pool.connection() as conn:
        await conn.execute(
            "UPDATE understanding_chunk_jobs SET claimed_at = now() - interval '10 minutes'"
        )
    await release_job(aops_pool, job.id, error="checkpoint behind")
    # meiva:note
    # just released: not due for the spacing
    assert await claim_job(aops_pool) is None


async def test_a_chunk_that_overlaps_existing_nodes_describes_only_what_is_left(
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, chunk_seams: ChunkSeams
) -> None:
    # meiva:note
    # A manual close and the producers' next size cut (or a replay after a restart) can cover
    # the same stretch: the later job is shortened to the undescribed part, and skipped when
    # nothing is left.
    async with aops_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO understanding_nodes (agent_id, depth, span_start, span_end, segment_key,"
            " text, text_hash, input_hash, children_count, model, engine_version, prompt_version,"
            " schema_version) VALUES (5, 1, 0, 0, 'k', 'old', 'h', 'i', 0, 'm', 'chunk-0.2', 'p', 1)"
        )
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await run_rounds(aops_pool, 1, model_catalog=model_catalog, seams=chunk_seams)
    # meiva:note
    # m0 was already covered
    assert [m.id for m in chunk_seams.state["described"][0].messages] == ["m1"]
    assert await written_nodes(aops_pool) == [(5, 1, 0, 0, "old"), (5, 1, 1, 1, "what happened")]
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 2), end_msg_id="m0")
    await run_rounds(aops_pool, 1, model_catalog=model_catalog, seams=chunk_seams)
    status = (await job_statuses(aops_pool))[-1]
    assert status[0] == "skipped" and "already described" in status[2]


async def test_existing_nodes_in_the_middle_leave_a_gap_that_is_reported_not_dropped(
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    chunk_seams: ChunkSeams,
    emitted_events: EmittedEvents,
) -> None:
    # meiva:note
    # A node covering the middle of a chunk splits it into two undescribed runs: the first is
    # described, the other is named in an event (the old prefix-only trim lost it silently).
    async with aops_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO understanding_nodes (agent_id, depth, span_start, span_end, segment_key,"
            " text, text_hash, input_hash, children_count, model, engine_version, prompt_version,"
            " schema_version) VALUES (5, 1, 1, 1, 'k', 'mid', 'h', 'i', 0, 'm', 'chunk-0.2', 'p', 1)"
        )
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 5), end_msg_id="m3")
    await run_rounds(aops_pool, 1, model_catalog=model_catalog, seams=chunk_seams)
    assert [m.id for m in chunk_seams.state["described"][0].messages] == ["m0"]
    assert {r[2:4] for r in await written_nodes(aops_pool)} == {(0, 0), (1, 1)}
    [(name, attrs)] = [(event.event_name, event.attributes) for event in emitted_events]
    assert attrs is not None
    assert name == "understanding_chunk_gap" and attrs["gaps"] == "2-3"


async def test_a_cancelled_job_is_failed_and_reported_not_put_back(
    chunk_timestamps: TimestampPolicy,
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    chunk_seams: ChunkSeams,
    emitted_events: EmittedEvents,
) -> None:
    # meiva:note
    # The host stops while a job runs (a rollout): the job ends failed at once, so it neither
    # holds its agent's queue for the 60-minute lease nor runs a second time.
    started = threading.Event()
    release = threading.Event()

    def hang(located: LocatedChunk) -> ChunkResult:
        started.set()
        release.wait(10)
        return chunk_result(located, timeline_inputs=chunk_timestamps)

    chunk_seams.state["answer"] = hang
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    consumer = loop.Consumer(
        aops_pool,
        MagicMock(),
        [],
        clock=CLOCK,
        **lm_wiring(model_catalog),
        config=consumer_config(),
        describe=chunk_seams.describe,
        load_segments=chunk_seams.load_segments,
    )
    task = asyncio.create_task(consumer.run_until_idle())
    try:
        assert await asyncio.to_thread(started.wait, 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    [(status, attempts, error)] = await job_statuses(aops_pool)
    assert (status, attempts, error) == ("failed", 1, "host stopping")
    assert emitted_events.names() == ["understanding_chunk_failed"]


async def test_the_give_up_clock_starts_at_the_first_wait_and_a_queued_job_has_none(
    aops_pool: AsyncConnectionPool,
) -> None:
    from base.agents.history.hierarchy.chunks import claim_job, release_job

    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    # meiva:note
    # queued for days while the feature was off
    async with aops_pool.connection() as conn:
        await conn.execute(
            "UPDATE understanding_chunk_jobs SET created_at = now() - interval '3 days'"
        )
    job = await claim_job(aops_pool)
    assert job is not None and job.age_seconds == 0.0
    await release_job(aops_pool, job.id, error="wait")
    async with aops_pool.connection() as conn:
        await conn.execute(
            "UPDATE understanding_chunk_jobs SET claimed_at = now() - interval '1 hour',"
            " waiting_since = now() - interval '2 hours'"
        )
    again = await claim_job(aops_pool)
    assert again is not None and 7000 < again.age_seconds < 7400
    # meiva:note
    # waiting spends no attempt
    assert again.attempts == 1
    await release_job(aops_pool, again.id, error="wait")
    async with aops_pool.connection() as conn:
        await conn.execute(
            "UPDATE understanding_chunk_jobs SET claimed_at = now() - interval '1 h'"
        )
    last = await claim_job(aops_pool)
    # meiva:note
    # a second wait keeps the clock
    assert last is not None and 7000 < last.age_seconds < 7400


async def test_a_job_that_never_becomes_describable_is_given_up_on_by_age(
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    emitted_events: EmittedEvents,
    chunk_seams: ChunkSeams,
) -> None:
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 9), end_msg_id="m8")
    async with aops_pool.connection() as conn:
        await conn.execute(
            "UPDATE understanding_chunk_jobs SET waiting_since = now() - interval '7 hours'"
        )
    await run_rounds(aops_pool, 1, model_catalog=model_catalog, seams=chunk_seams)
    [(status, _, error)] = await job_statuses(aops_pool)
    assert status == "failed" and "gave up" in error


async def test_a_closing_chunk_past_its_snapshot_describes_what_exists_and_reports_the_rest(
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    chunk_seams: ChunkSeams,
    emitted_events: EmittedEvents,
) -> None:
    # meiva:note
    # The boundary snapshot of segment 0 holds fewer messages than the closing chunk's end (the
    # checkpoint of the segment's last super-step was still in flight at compaction). The part the
    # snapshot holds is described and written; the missing request indices are in the event.
    chunk_seams.state["closing_segment"] = 0
    await enqueue_chunk(
        aops_pool,
        5,
        compact_version=0,
        chunk=Chunk(1, 9),
        end_msg_id="m8",
        boundary_checkpoint_id="cp-1",
    )
    await run_rounds(aops_pool, 1, model_catalog=model_catalog, seams=chunk_seams)
    [(status, _, error)] = await job_statuses(aops_pool)
    assert (status, error) == ("done", None)
    assert [m.id for m in chunk_seams.state["described"][0].messages] == ["m0", "m1", "m2", "m3"]
    assert len(await written_nodes(aops_pool)) == 1
    [(name, attrs)] = [(event.event_name, event.attributes) for event in emitted_events]
    assert attrs is not None and name == "understanding_chunk_gap"
    assert "request 5-9 (not in the snapshot)" in attrs["gaps"]
