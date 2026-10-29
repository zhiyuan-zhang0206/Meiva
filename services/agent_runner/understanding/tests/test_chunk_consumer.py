# meiva:note
# These existing fault tests use heterogeneous spy state at precise failure boundaries.
# pyright: reportUnknownMemberType = warning
# pyright: reportUnknownArgumentType = warning
# pyright: reportUnknownLambdaType = warning
# pyright: reportUnknownVariableType = warning
# meiva:note
# The understanding consumer loop: claim, locate, describe, store, settle.
from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import MagicMock

import psycopg
import pytest
from langchain_core.messages import AIMessage, SystemMessage
from psycopg_pool import AsyncConnectionPool

from base.agents.history.checkpoint import CheckpointReadError
from base.agents.history.hierarchy.chunk_generate import ChunkResult
from base.agents.history.hierarchy.chunks import (
    Chunk,
    ChunkCall,
    ChunkJob,
    LocatedChunk,
    enqueue_chunk,
)
from base.agents.history.hierarchy.generate import GenerateError
from base.agents.history.hierarchy.leaf_groups import UnitGroup
from base.agents.messages.timestamp_policy import TimestampPolicy
from base.clock.tests.fakes import system_clock
from base.config import ConfigBoot
from base.host.env.agent_slices import ModelOverrides
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
    GroupModels,
    consumer_config,
    lm_wiring,
)
from services.agent_runner.understanding.tests.consumer_history import (
    call_receipts,
    job_spans,
    job_statuses,
    notes_history,
    written_nodes,
)

# meiva:note
# One clock for every consumer in this file: the value a host builds once at its entry and hands
# to each loop, so the tests pass what production passes.
CLOCK = system_clock()


async def test_round_describes_a_due_chunk_and_stores_its_node(
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, chunk_seams: ChunkSeams
) -> None:
    # meiva:note
    # Request list [head, m0, m1, m2]; chunk [1, 3) = m0, m1.
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await run_rounds(aops_pool, 1, model_catalog=model_catalog, seams=chunk_seams)
    assert await written_nodes(aops_pool) == [(5, 1, 0, 1, "what happened")]
    assert await job_statuses(aops_pool) == [("done", 1, None)]
    assert [m.id for m in chunk_seams.state["described"][0].messages] == ["m0", "m1"]
    # meiva:note
    # queue drained: nothing more to claim
    await run_rounds(aops_pool, 1, model_catalog=model_catalog, seams=chunk_seams)
    assert await job_statuses(aops_pool) == [("done", 1, None)]


async def test_chunk_not_yet_checkpointed_goes_back_to_the_queue(
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    chunk_seams: ChunkSeams,
) -> None:
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 9), end_msg_id="m8")
    await run_rounds(aops_pool, 1, model_catalog=model_catalog, seams=chunk_seams)
    [(status, attempts, error)] = await job_statuses(aops_pool)
    # meiva:note
    # waiting spends no attempt
    assert (status, attempts) == ("pending", 0) and "shorter" in error
    assert await written_nodes(aops_pool) == []


async def test_drifted_indices_fail_the_job_with_an_event(
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    emitted_events: EmittedEvents,
    chunk_seams: ChunkSeams,
) -> None:
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="not-m1")
    await run_rounds(aops_pool, 1, model_catalog=model_catalog, seams=chunk_seams)
    [(status, _, error)] = await job_statuses(aops_pool)
    assert status == "failed" and "not-m1" in error
    assert "understanding_chunk_failed" in emitted_events.names()


async def test_a_database_blink_fails_the_job_with_an_event_and_is_not_retried(
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    chunk_seams: ChunkSeams,
    emitted_events: EmittedEvents,
) -> None:
    # meiva:note
    # A chunk is attempted once: a database error ends it as failed (and reported), it does not
    # go back to the queue.
    chunk_seams.state["segments_error"] = psycopg.OperationalError(
        "the database closed the connection"
    )
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await run_rounds(aops_pool, 2, model_catalog=model_catalog, seams=chunk_seams)
    [(status, attempts, error)] = await job_statuses(aops_pool)
    assert (status, attempts) == ("failed", 1) and "closed the connection" in error
    assert emitted_events.names() == ["understanding_chunk_failed"]


async def test_generation_error_fails_on_the_first_attempt_with_an_event(
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    chunk_seams: ChunkSeams,
    emitted_events: EmittedEvents,
) -> None:
    chunk_seams.state["error"] = GenerateError("provider down")
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await run_rounds(aops_pool, 1, model_catalog=model_catalog, seams=chunk_seams)
    [(status, attempts, error)] = await job_statuses(aops_pool)
    assert (status, attempts, error) == ("failed", 1, "provider down")
    assert emitted_events.names() == ["understanding_chunk_failed"]
    # meiva:note
    # nothing makes it due again
    async with aops_pool.connection() as conn:
        await conn.execute(
            "UPDATE understanding_chunk_jobs SET claimed_at = now() - interval '1 hour'"
        )
    await run_rounds(aops_pool, 2, model_catalog=model_catalog, seams=chunk_seams)
    assert await job_statuses(aops_pool) == [("failed", 1, "provider down")]


async def test_a_claimer_that_died_fails_its_job_and_frees_the_agents_queue(
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    emitted_events: EmittedEvents,
    chunk_seams: ChunkSeams,
) -> None:
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(3, 5), end_msg_id="m3")
    # meiva:note
    # its claimer "dies": nothing ever settles it
    first = await loop.claim_job(aops_pool)
    assert first is not None and first.end_index == 3
    async with aops_pool.connection() as conn:
        await conn.execute(
            "UPDATE understanding_chunk_jobs SET claimed_at = now() - interval '2 hours'"
            " WHERE id = %s",
            (first.id,),
        )
    await run_rounds(aops_pool, 1, model_catalog=model_catalog, seams=chunk_seams)
    assert [row[0] for row in await job_statuses(aops_pool)] == ["failed", "done"]
    assert emitted_events.names() == ["understanding_chunk_failed"]


async def test_gemini_chunk_is_described_with_its_original_prefix(
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    chunk_seams: ChunkSeams,
) -> None:
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await run_rounds(
        aops_pool, 1, model_catalog=model_catalog, seams=chunk_seams, model="gemini-3.8-flash"
    )
    assert await job_statuses(aops_pool) == [("done", 1, None)]
    assert await written_nodes(aops_pool) == [(5, 1, 0, 1, "what happened")]
    located = chunk_seams.state["described"][0]
    assert located.prefix[0] == SystemMessage(content="head")
    assert [msg.id for msg in located.prefix[1:]] == ["m0", "m1"]


async def test_checkpoint_read_failure_fails_the_job(
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    chunk_seams: ChunkSeams,
    emitted_events: EmittedEvents,
) -> None:
    chunk_seams.state["segments_error"] = CheckpointReadError("db blip")
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await run_rounds(aops_pool, 1, model_catalog=model_catalog, seams=chunk_seams)
    [(status, _, error)] = await job_statuses(aops_pool)
    assert status == "failed" and "db blip" in error


async def test_loop_is_idle_when_the_feature_is_off(
    model_catalog: ModelCatalog,
) -> None:
    config = consumer_config()
    config.set_field("understanding_enabled", False)
    # meiva:note
    # returns at once
    await loop.understanding_loop_forever(
        MagicMock(),
        MagicMock(),
        [],
        clock=CLOCK,
        **lm_wiring(model_catalog),
        config=config,
    )


def test_generation_models_are_reused_and_never_closed(
    model_catalog: ModelCatalog,
) -> None:
    from dataclasses import replace

    from base.lm.provider_api import BuildContext, ProviderBinding

    built: list[tuple[MagicMock, BuildContext]] = []
    closed: list[str] = []

    def build(ctx: BuildContext) -> MagicMock:
        llm = MagicMock(name=ctx.model, close=lambda: closed.append(ctx.model))
        built.append((llm, ctx))
        return llm

    binding = ProviderBinding(
        prefix="deepseek-",
        display_name="Local SDK transport",
        key_env="DEEPSEEK_API_KEY",
        build=build,
        effort_levels=("high", "max"),
        provider_key="openai",
    )
    catalog = replace(model_catalog, bindings={**model_catalog.bindings, "deepseek-": binding})
    models = loop.ModelCache(**lm_wiring(catalog))
    none = ModelOverrides.from_pins(None)
    first = models.get("deepseek-flash", none)
    assert first is built[0][0] and models.get("deepseek-flash", none) is first
    assert models.get("deepseek-reasoner", none) is not first
    assert len(built) == 2 and closed == []
    # meiva:note
    # the grouping reasoning knob: empty leaves the model's tier alone, `off` disables
    # thinking, anything else is the effort asked; each is its own cached model.
    off, high = (
        models.get("deepseek-flash", none, "off"),
        models.get("deepseek-flash", none, "high"),
    )
    assert off is built[2][0] and high is built[3][0]
    assert off is not first and high is not first and off is not high
    assert built[0][1].thinking is None
    assert built[2][1].thinking == {"type": "disabled"}
    assert built[3][1].thinking is None and built[3][1].resolved_effort == "high"


def _call(round_: int, response: AIMessage | None, error: str | None = None) -> ChunkCall:
    return ChunkCall(
        round=round_,
        model="deepseek-flash",
        instruction="describe it",
        prefix_len=3,
        start_offset=1,
        response=response,
        duration_ms=12.5,
        error=error,
    )


async def test_raw_calls_are_persisted_for_a_done_job(
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    chunk_seams: ChunkSeams,
) -> None:
    reply = AIMessage(
        content=[{"type": "thinking", "thinking": "hm"}, {"type": "text", "text": "what"}],
        usage_metadata={"input_tokens": 9, "output_tokens": 2, "total_tokens": 11},
        response_metadata={"stop_reason": "end_turn"},
    )
    chunk_seams.state["calls"] = [_call(0, reply)]
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await run_rounds(aops_pool, 1, model_catalog=model_catalog, seams=chunk_seams)
    [row] = await call_receipts(aops_pool)
    async with aops_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT id FROM understanding_chunk_jobs")
        [(job_id,)] = await cur.fetchall()
    assert row[:8] == (job_id, 5, 1, 0, "deepseek-flash", "describe it", 3, 1)
    assert row[8] == [{"type": "thinking", "thinking": "hm"}, {"type": "text", "text": "what"}]
    assert row[10]["input_tokens"] == 9 and row[11] == {"stop_reason": "end_turn"}
    assert row[12:] == (12.5, None)


async def test_a_failed_call_is_recorded_and_the_job_fails(
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    chunk_seams: ChunkSeams,
    emitted_events: EmittedEvents,
) -> None:
    chunk_seams.state["calls"] = [_call(0, None, "provider down")]
    chunk_seams.state["error"] = GenerateError("provider down")
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await run_rounds(aops_pool, 1, model_catalog=model_catalog, seams=chunk_seams)
    [row] = await call_receipts(aops_pool)
    assert row[8] is None and row[13] == "provider down"
    assert (await job_statuses(aops_pool))[0][0] == "failed"


async def test_a_record_write_failure_emits_an_event_and_spares_the_job(
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    chunk_seams: ChunkSeams,
    emitted_events: EmittedEvents,
) -> None:
    chunk_seams.state["calls"] = [_call(2**40, AIMessage(content="x"))]
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await run_rounds(aops_pool, 1, model_catalog=model_catalog, seams=chunk_seams)
    assert emitted_events.names() == ["understanding_call_record_failed"]
    assert await job_statuses(aops_pool) == [("done", 1, None)]
    assert await written_nodes(aops_pool) == [(5, 1, 0, 1, "what happened")]


async def test_each_group_is_a_node_covering_the_chunk_to_its_end(
    chunk_timestamps: TimestampPolicy,
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    chunk_seams: ChunkSeams,
) -> None:
    # meiva:note
    # Request [head, m0..m3]; chunk [1, 5): four units in three groups.
    chunk_seams.state["answer"] = lambda located: chunk_result(
        located,
        UnitGroup(0, 0, "first"),
        UnitGroup(1, 2, "second"),
        UnitGroup(3, 3, "third"),
        timeline_inputs=chunk_timestamps,
    )
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 5), end_msg_id="m3")
    await run_rounds(aops_pool, 1, model_catalog=model_catalog, seams=chunk_seams)
    assert await written_nodes(aops_pool) == [
        (5, 1, 0, 0, "first"),
        (5, 1, 1, 2, "second"),
        (5, 1, 3, 3, "third"),
    ]
    assert await job_spans(aops_pool) == [("done", 1, 5)]
    assert set(chunk_seams.state["kw"]) == {"clock", "config"}
    assert isinstance(chunk_seams.state["kw"]["config"], ConfigBoot)


async def test_each_chunk_starts_at_its_own_start_whatever_the_previous_one_did(
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, chunk_seams: ChunkSeams
) -> None:
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(3, 5), end_msg_id="m3")
    await run_rounds(aops_pool, 2, model_catalog=model_catalog, seams=chunk_seams)
    assert [[m.id for m in loc.messages] for loc in chunk_seams.state["described"]] == [
        ["m0", "m1"],
        ["m2", "m3"],
    ]
    assert await written_nodes(aops_pool) == [
        (5, 1, 0, 1, "what happened"),
        (5, 1, 2, 3, "what happened"),
    ]


async def test_a_refused_grouping_fails_the_job_at_once_without_nodes(
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, chunk_seams: ChunkSeams
) -> None:
    chunk_seams.state["error"] = GenerateError(
        "grouping reply refused after 2 correction(s): no <groups>"
    )
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await run_rounds(aops_pool, 1, model_catalog=model_catalog, seams=chunk_seams)
    [(status, attempts, error)] = await job_statuses(aops_pool)
    assert (status, attempts) == ("failed", 1) and "refused" in error
    assert await written_nodes(aops_pool) == []


async def test_a_done_job_is_followed_by_the_upper_level_checks(
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    chunk_seams: ChunkSeams,
) -> None:
    # meiva:note
    # `GroupModels` satisfies `InvocableLlm` for `run_group_checks` without a real SDK client.
    from dataclasses import replace

    from base.lm.provider_api import ProviderBinding

    state: dict[str, object] = {}
    group_models = GroupModels(model_catalog, state)
    binding = ProviderBinding(
        prefix="deepseek-",
        display_name="Local SDK transport",
        key_env="DEEPSEEK_API_KEY",
        build=lambda _ctx: group_models,  # pyright: ignore[reportArgumentType]
        effort_levels=("high", "max"),
        provider_key="openai",
    )
    catalog = replace(model_catalog, bindings={**model_catalog.bindings, "deepseek-": binding})
    async with aops_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO understanding_nodes (agent_id, depth, span_start, span_end, start_ts,"
            " end_ts, segment_key, text, text_hash, input_hash, children_count, model,"
            " engine_version, prompt_version, schema_version)"
            " SELECT 5, 1, i*10, i*10+9, '2026-10-05T00:00Z'::timestamptz,"
            " '2026-10-05T00:30Z'::timestamptz, 'k', 'leaf', 'h', 'i', 0, 'm',"
            " 'chunk-0.2', 'p', 1 FROM generate_series(1, 5) AS i"
        )
    config = consumer_config()
    config.set_field("understanding_group_model", "deepseek-flash")
    config.set_field("understanding_group_check_open", 6)
    config.set_field("understanding_group_check_decay", 1)
    config.set_field("understanding_group_corrections", 0)
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await loop.Consumer(
        aops_pool,
        MagicMock(),
        [],
        clock=CLOCK,
        **lm_wiring(catalog),
        config=config,
        describe=chunk_seams.describe,
        load_segments=chunk_seams.load_segments,
    ).run_until_idle()
    async with aops_pool.connection() as conn:
        rows = await (
            await conn.execute("SELECT agent_id, last_checked_open FROM understanding_group_state")
        ).fetchall()
    assert rows == [(5, 6)]


async def _enqueue_ends(pool: AsyncConnectionPool, agent: int, *ends: str) -> None:
    # meiva:note
    # Jobs of one agent over the test history (request [head, m0..m3]); `m1` ends chunk (1, 3).
    spans = {"m1": Chunk(1, 3), "m2": Chunk(1, 4), "m3": Chunk(3, 5)}
    for end in ends:
        await enqueue_chunk(pool, agent, compact_version=0, chunk=spans[end], end_msg_id=end)


async def test_different_agents_run_together_and_one_agents_jobs_in_order(
    chunk_timestamps: TimestampPolicy,
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    chunk_seams: ChunkSeams,
) -> None:
    log: list[tuple[str, str]] = []
    lock = threading.Lock()
    b_started = threading.Event()

    def answer(located: LocatedChunk) -> ChunkResult:
        tag = {"m1": "a1", "m2": "b", "m3": "a2"}[str(located.messages[-1].id)]
        with lock:
            log.append(("start", tag))
        if tag == "b":
            b_started.set()
        if tag == "a1":
            assert b_started.wait(10), "agent b never ran while agent a's first job was in flight"
        time.sleep(0.05)
        with lock:
            log.append(("end", tag))
        return chunk_result(located, timeline_inputs=chunk_timestamps)

    chunk_seams.state["answer"] = answer
    # meiva:note
    # agent 1: a1 then a2
    await _enqueue_ends(aops_pool, 1, "m1", "m3")
    # meiva:note
    # agent 2: b
    await _enqueue_ends(aops_pool, 2, "m2")
    await loop.Consumer(
        aops_pool,
        MagicMock(),
        [],
        clock=CLOCK,
        **lm_wiring(model_catalog),
        config=consumer_config(),
        describe=chunk_seams.describe,
        load_segments=chunk_seams.load_segments,
    ).run_until_idle()
    assert [s for s, *_ in await job_statuses(aops_pool)] == ["done"] * 3
    # meiva:note
    # overlapped
    assert log.index(("start", "b")) < log.index(("end", "a1"))
    # meiva:note
    # the same agent, in order
    assert log.index(("end", "a1")) < log.index(("start", "a2"))


async def test_every_due_job_runs_at_once_each_on_its_own_thread(
    chunk_timestamps: TimestampPolicy,
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    chunk_seams: ChunkSeams,
) -> None:
    barrier = threading.Barrier(5, timeout=10)

    def answer(located: LocatedChunk) -> ChunkResult:
        # meiva:note
        # passes only when all five jobs are inside their calls together
        barrier.wait()
        return chunk_result(located, timeline_inputs=chunk_timestamps)

    chunk_seams.state["answer"] = answer
    for agent in range(1, 6):
        await _enqueue_ends(aops_pool, agent, "m1")
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
    await consumer.run_until_idle()
    jobs = await job_statuses(aops_pool)
    assert [s for s, *_ in jobs] == ["done"] * 5, jobs
    assert consumer.in_flight == 0


async def test_one_jobs_failure_touches_no_other_job(
    chunk_timestamps: TimestampPolicy,
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    chunk_seams: ChunkSeams,
) -> None:
    def answer(located: LocatedChunk) -> ChunkResult:
        if located.messages[-1].id == "m2":
            # meiva:note
            # not a GenerateError: the crash path
            raise RuntimeError("bug in one job")
        return chunk_result(located, timeline_inputs=chunk_timestamps)

    chunk_seams.state["answer"] = answer
    for agent, end in ((1, "m1"), (2, "m2"), (3, "m1")):
        await _enqueue_ends(aops_pool, agent, end)
    await loop.Consumer(
        aops_pool,
        MagicMock(),
        [],
        clock=CLOCK,
        **lm_wiring(model_catalog),
        config=consumer_config(),
        describe=chunk_seams.describe,
        load_segments=chunk_seams.load_segments,
    ).run_until_idle()
    status = [(s, e) for s, _, e in await job_statuses(aops_pool)]
    assert [s for s, _ in status] == ["done", "failed", "done"]
    assert "bug in one job" in str(status[1][1])


async def test_a_job_that_cannot_be_settled_is_left_to_its_lease_and_others_finish(
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    chunk_seams: ChunkSeams,
) -> None:
    real = loop.settle_chunk_job

    async def settle(pool: AsyncConnectionPool, job: ChunkJob, outcome: loop.Outcome) -> None:
        if job.agent_id == 1:
            raise ConnectionError("database went away")
        await real(pool, job, outcome)

    for agent in (1, 2):
        await _enqueue_ends(aops_pool, agent, "m1")
    consumer = loop.Consumer(
        aops_pool,
        MagicMock(),
        [],
        clock=CLOCK,
        **lm_wiring(model_catalog),
        config=consumer_config(),
        describe=chunk_seams.describe,
        load_segments=chunk_seams.load_segments,
        settle_chunk_job=settle,
    )
    # meiva:note
    # does not raise
    await consumer.run_until_idle()
    assert [s for s, *_ in await job_statuses(aops_pool)] == ["running", "done"]
    assert consumer.in_flight == 0


async def test_the_backlog_event_carries_in_flight(
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    emitted_events: EmittedEvents,
    chunk_seams: ChunkSeams,
) -> None:
    await _enqueue_ends(aops_pool, 1, "m1")
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
    consumer.in_flight = 1
    await consumer.emit_backlog()
    assert [event.attributes for event in emitted_events][-1]["pending"] == 1 and [
        event.attributes for event in emitted_events
    ][-1]["in_flight"] == 1


async def test_the_forever_loop_works_the_queue_and_stops_cleanly_on_cancel(
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, chunk_seams: ChunkSeams
) -> None:
    config = consumer_config()
    config.set_field("understanding_enabled", True)
    for agent in (1, 2, 3):
        await _enqueue_ends(aops_pool, agent, "m1")
    task = asyncio.create_task(
        loop.understanding_loop_forever(
            aops_pool,
            MagicMock(),
            [],
            clock=CLOCK,
            **lm_wiring(model_catalog),
            config=config,
            poll_seconds=0.05,
            describe=chunk_seams.describe,
            load_segments=chunk_seams.load_segments,
        )
    )
    for _ in range(100):
        if [s for s, *_ in await job_statuses(aops_pool)] == ["done"] * 3:
            break
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    jobs = await job_statuses(aops_pool)
    assert [s for s, *_ in jobs] == ["done"] * 3, jobs


async def test_a_replay_describes_one_agents_segments_together_and_skips_the_upper_checks(
    chunk_timestamps: TimestampPolicy,
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    chunk_seams: ChunkSeams,
) -> None:
    # meiva:note
    # passes only when both segments are in flight
    both = threading.Barrier(2, timeout=10)

    def answer(located: LocatedChunk) -> ChunkResult:
        both.wait()
        return chunk_result(located, timeline_inputs=chunk_timestamps)

    chunk_seams.state["answer"] = answer
    async with aops_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO understanding_nodes (agent_id, depth, span_start, span_end, start_ts,"
            " end_ts, segment_key, text, text_hash, input_hash, children_count, model,"
            " engine_version, prompt_version, schema_version)"
            " SELECT 1, 1, i*10, i*10+9, '2026-10-05T00:00Z'::timestamptz,"
            " '2026-10-05T00:30Z'::timestamptz, 'k', 'leaf', 'h', 'i', 0, 'm',"
            " 'chunk-0.2', 'p', 1 FROM generate_series(1, 5) AS i"
        )
    for segment in (0, 1):
        await enqueue_chunk(
            aops_pool, 1, compact_version=segment, chunk=Chunk(1, 3), end_msg_id="m1"
        )
    await enqueue_chunk(aops_pool, 2, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    config = consumer_config()
    config.set_field("understanding_group_check_open", 6)
    await loop.replay_jobs(
        aops_pool,
        MagicMock(),
        [],
        1,
        clock=CLOCK,
        **lm_wiring(model_catalog),
        config=config,
        describe=chunk_seams.describe,
        load_segments=chunk_seams.load_segments,
    )
    assert [s for s, *_ in await job_statuses(aops_pool)] == ["done", "done", "pending"]
    async with aops_pool.connection() as conn:
        rows = await (await conn.execute("SELECT 1 FROM understanding_group_state")).fetchall()
    assert rows == []


async def test_a_chunk_of_only_framework_notes_is_skipped_without_a_call(
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, chunk_seams: ChunkSeams
) -> None:
    chunk_seams.state["history"] = notes_history()
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await run_rounds(aops_pool, 1, model_catalog=model_catalog, seams=chunk_seams)
    [(status, _, error)] = await job_statuses(aops_pool)
    assert status == "skipped" and "only framework notes" in error
    assert chunk_seams.state["described"] == [] and await written_nodes(aops_pool) == []
