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
from base.config import ConfigBoot, settings
from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog
from base.lm.catalog.provider_keys import ProviderKeys
from base.telemetry.tests.capture.event_capture import EmittedEvents
from services.agent_runner.understanding import chunk_consumer as loop
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
    call_receipts,
    inbound_history,
    job_spans,
    job_statuses,
    notes_history,
    written_nodes,
)


async def test_round_describes_a_due_chunk_and_stores_its_node(
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, chunk_seams: dict
) -> None:
    # meiva:note
    # Request list [head, m0, m1, m2]; chunk [1, 3) = m0, m1.
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await run_rounds(aops_pool, 1, model_catalog=model_catalog)
    assert await written_nodes(aops_pool) == [(5, 1, 0, 1, "what happened")]
    assert await job_statuses(aops_pool) == [("done", 1, None)]
    assert [m.id for m in chunk_seams["described"][0].messages] == ["m0", "m1"]
    # meiva:note
    # queue drained: nothing more to claim
    await run_rounds(aops_pool, 1, model_catalog=model_catalog)
    assert await job_statuses(aops_pool) == [("done", 1, None)]


async def test_chunk_not_yet_checkpointed_goes_back_to_the_queue(
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
) -> None:
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 9), end_msg_id="m8")
    await run_rounds(aops_pool, 1, model_catalog=model_catalog)
    [(status, attempts, error)] = await job_statuses(aops_pool)
    # meiva:note
    # waiting spends no attempt
    assert (status, attempts) == ("pending", 0) and "shorter" in error
    assert await written_nodes(aops_pool) == []


async def test_drifted_indices_fail_the_job_with_an_event(
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, emitted_events: EmittedEvents
) -> None:
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="not-m1")
    await run_rounds(aops_pool, 1, model_catalog=model_catalog)
    [(status, _, error)] = await job_statuses(aops_pool)
    assert status == "failed" and "not-m1" in error
    assert "understanding_chunk_failed" in emitted_events.names()


async def test_a_database_blink_fails_the_job_with_an_event_and_is_not_retried(
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    emitted_events: EmittedEvents,
) -> None:
    # meiva:note
    # A chunk is attempted once: a database error ends it as failed (and reported), it does not
    # go back to the queue.

    def blink(*_a: object) -> object:
        raise psycopg.OperationalError("the database closed the connection")

    monkeypatch.setattr(loop, "load_segments", blink)
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await run_rounds(aops_pool, 2, model_catalog=model_catalog)
    [(status, attempts, error)] = await job_statuses(aops_pool)
    assert (status, attempts) == ("failed", 1) and "closed the connection" in error
    assert emitted_events.names() == ["understanding_chunk_failed"]


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
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, chunk_seams: dict
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
    await run_rounds(aops_pool, 1, model_catalog=model_catalog)
    # meiva:note
    # m0 was already covered
    assert [m.id for m in chunk_seams["described"][0].messages] == ["m1"]
    assert await written_nodes(aops_pool) == [(5, 1, 0, 0, "old"), (5, 1, 1, 1, "what happened")]
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 2), end_msg_id="m0")
    await run_rounds(aops_pool, 1, model_catalog=model_catalog)
    status = (await job_statuses(aops_pool))[-1]
    assert status[0] == "skipped" and "already described" in status[2]


async def test_existing_nodes_in_the_middle_leave_a_gap_that_is_reported_not_dropped(
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    chunk_seams: dict,
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
    await run_rounds(aops_pool, 1, model_catalog=model_catalog)
    assert [m.id for m in chunk_seams["described"][0].messages] == ["m0"]
    assert {r[2:4] for r in await written_nodes(aops_pool)} == {(0, 0), (1, 1)}
    [(name, attrs)] = [(event.event_name, event.attributes) for event in emitted_events]
    assert attrs is not None
    assert name == "understanding_chunk_gap" and attrs["gaps"] == "2-3"


async def test_a_cancelled_job_is_failed_and_reported_not_put_back(
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    emitted_events: EmittedEvents,
) -> None:
    # meiva:note
    # The host stops while a job runs (a rollout): the job ends failed at once, so it neither
    # holds its agent's queue for the 60-minute lease nor runs a second time.
    started = asyncio.Event()

    async def hang(*_a: object, **_k: object) -> loop.Outcome:
        started.set()
        await asyncio.sleep(3600)
        return loop.Outcome("done")

    monkeypatch.setattr(loop, "run_job", hang)
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    consumer = loop.Consumer(
        aops_pool, MagicMock(), [], **lm_wiring(model_catalog), config=consumer_config()
    )
    task = asyncio.create_task(consumer.run_until_idle())
    await asyncio.wait_for(started.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
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
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, emitted_events: EmittedEvents
) -> None:
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 9), end_msg_id="m8")
    async with aops_pool.connection() as conn:
        await conn.execute(
            "UPDATE understanding_chunk_jobs SET waiting_since = now() - interval '7 hours'"
        )
    await run_rounds(aops_pool, 1, model_catalog=model_catalog)
    [(status, _, error)] = await job_statuses(aops_pool)
    assert status == "failed" and "gave up" in error


async def test_a_closing_chunk_past_its_snapshot_describes_what_exists_and_reports_the_rest(
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    chunk_seams: dict,
    emitted_events: EmittedEvents,
) -> None:
    # meiva:note
    # The boundary snapshot of segment 0 holds fewer messages than the closing chunk's end (the
    # checkpoint of the segment's last super-step was still in flight at compaction). The part the
    # snapshot holds is described and written; the missing request indices are in the event.
    monkeypatch.setattr(loop, "load_segments", lambda *_a: (inbound_history(), 0))
    await enqueue_chunk(
        aops_pool,
        5,
        compact_version=0,
        chunk=Chunk(1, 9),
        end_msg_id="m8",
        boundary_checkpoint_id="cp-1",
    )
    await run_rounds(aops_pool, 1, model_catalog=model_catalog)
    [(status, _, error)] = await job_statuses(aops_pool)
    assert (status, error) == ("done", None)
    assert [m.id for m in chunk_seams["described"][0].messages] == ["m0", "m1", "m2", "m3"]
    assert len(await written_nodes(aops_pool)) == 1
    [(name, attrs)] = [(event.event_name, event.attributes) for event in emitted_events]
    assert attrs is not None and name == "understanding_chunk_gap"
    assert "request 5-9 (not in the snapshot)" in attrs["gaps"]


async def test_generation_error_fails_on_the_first_attempt_with_an_event(
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    emitted_events: EmittedEvents,
) -> None:

    def boom(*_a: object, **_k: object) -> str:
        raise GenerateError("provider down")

    monkeypatch.setattr(loop, "describe", boom)
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await run_rounds(aops_pool, 1, model_catalog=model_catalog)
    [(status, attempts, error)] = await job_statuses(aops_pool)
    assert (status, attempts, error) == ("failed", 1, "provider down")
    assert emitted_events.names() == ["understanding_chunk_failed"]
    # meiva:note
    # nothing makes it due again
    async with aops_pool.connection() as conn:
        await conn.execute(
            "UPDATE understanding_chunk_jobs SET claimed_at = now() - interval '1 hour'"
        )
    await run_rounds(aops_pool, 2, model_catalog=model_catalog)
    assert await job_statuses(aops_pool) == [("failed", 1, "provider down")]


async def test_a_claimer_that_died_fails_its_job_and_frees_the_agents_queue(
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, emitted_events: EmittedEvents
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
    await run_rounds(aops_pool, 1, model_catalog=model_catalog)
    assert [row[0] for row in await job_statuses(aops_pool)] == ["failed", "done"]
    assert emitted_events.names() == ["understanding_chunk_failed"]


async def test_gemini_chunk_is_described_with_its_original_prefix(
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    chunk_seams: dict,
) -> None:
    monkeypatch.setattr(
        loop,
        "agent_model_target",
        lambda *_a, **_kw: ("gemini-3.8-flash", ModelOverrides.from_pins(None)),
    )
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await run_rounds(aops_pool, 1, model_catalog=model_catalog)
    assert await job_statuses(aops_pool) == [("done", 1, None)]
    assert await written_nodes(aops_pool) == [(5, 1, 0, 1, "what happened")]
    located = chunk_seams["described"][0]
    assert located.prefix[0] == SystemMessage(content="head")
    assert [msg.id for msg in located.prefix[1:]] == ["m0", "m1"]


async def test_checkpoint_read_failure_fails_the_job(
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    emitted_events: EmittedEvents,
) -> None:

    def fail(*_a: object) -> None:
        raise CheckpointReadError("db blip")

    monkeypatch.setattr(loop, "load_segments", fail)
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await run_rounds(aops_pool, 1, model_catalog=model_catalog)
    [(status, _, error)] = await job_statuses(aops_pool)
    assert status == "failed" and "db blip" in error


async def test_loop_is_idle_when_the_feature_is_off(
    model_catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.agent, "understanding_enabled", False)
    # meiva:note
    # returns at once
    await loop.understanding_loop_forever(
        MagicMock(), MagicMock(), [], **lm_wiring(model_catalog), config=consumer_config()
    )


def test_generation_models_are_reused_and_never_closed(
    model_catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[MagicMock] = []
    closed: list[str] = []

    def build(
        model: str,
        params: object,
        overrides: object,
        *,
        catalog: ModelCatalog,
        keys: ProviderKeys,
        llm_override: str,
        thinking_off: bool,
    ) -> MagicMock:
        assert catalog is model_catalog and llm_override == ""
        built.append(llm := MagicMock(name=model, close=lambda: closed.append(model)))
        llm.thinking_off, llm.params = thinking_off, params
        return llm

    monkeypatch.setattr(loop, "build_generation_llm", build)
    models = loop.ModelCache(**lm_wiring(model_catalog))
    none = ModelOverrides.from_pins(None)
    first = models.get("m", none)
    assert first is built[0] and models.get("m", none) is first
    assert models.get("other", none) is not first
    assert len(built) == 2 and closed == []
    # meiva:note
    # the grouping reasoning knob: empty leaves the model's tier alone, `off` disables
    # thinking, anything else is the effort asked; each is its own cached model.
    off, high = models.get("m", none, "off"), models.get("m", none, "high")
    assert off is built[2] and high is built[3]
    assert off is not first and high is not first and off is not high
    assert built[0].thinking_off is False and built[0].params is None
    assert built[2].thinking_off is True and built[2].params is None
    assert built[3].thinking_off is False and built[3].params.reasoning_effort == "high"


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
    chunk_timestamps: TimestampPolicy,
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    chunk_seams: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reply = AIMessage(
        content=[{"type": "thinking", "thinking": "hm"}, {"type": "text", "text": "what"}],
        usage_metadata={"input_tokens": 9, "output_tokens": 2, "total_tokens": 11},
        response_metadata={"stop_reason": "end_turn"},
    )

    def describe(*args: object, **_k: object) -> ChunkResult:
        calls, located = args[5], args[3]
        assert isinstance(calls, list) and isinstance(located, LocatedChunk)
        calls.append(_call(0, reply))
        return chunk_result(located, timeline_inputs=chunk_timestamps)

    monkeypatch.setattr(loop, "describe", describe)
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await run_rounds(aops_pool, 1, model_catalog=model_catalog)
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
    monkeypatch: pytest.MonkeyPatch,
    emitted_events: EmittedEvents,
) -> None:

    def boom(*args: object, **_k: object) -> str:
        calls = args[5]
        assert isinstance(calls, list)
        calls.append(_call(0, None, "provider down"))
        raise GenerateError("provider down")

    monkeypatch.setattr(loop, "describe", boom)
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await run_rounds(aops_pool, 1, model_catalog=model_catalog)
    [row] = await call_receipts(aops_pool)
    assert row[8] is None and row[13] == "provider down"
    assert (await job_statuses(aops_pool))[0][0] == "failed"


async def test_a_record_write_failure_emits_an_event_and_spares_the_job(
    chunk_timestamps: TimestampPolicy,
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    chunk_seams: dict,
    monkeypatch: pytest.MonkeyPatch,
    emitted_events: EmittedEvents,
) -> None:
    from base.agents.history.hierarchy import chunks

    monkeypatch.setattr(chunks, "INSERT_CALL_SQL", "INSERT INTO no_such_table VALUES (1)")

    def describe(*args: object, **_k: object) -> ChunkResult:
        calls, located = args[5], args[3]
        assert isinstance(calls, list) and isinstance(located, LocatedChunk)
        calls.append(_call(0, AIMessage(content="x")))
        return chunk_result(located, timeline_inputs=chunk_timestamps)

    monkeypatch.setattr(loop, "describe", describe)
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await run_rounds(aops_pool, 1, model_catalog=model_catalog)
    assert emitted_events.names() == ["understanding_call_record_failed"]
    assert await job_statuses(aops_pool) == [("done", 1, None)]
    assert await written_nodes(aops_pool) == [(5, 1, 0, 1, "what happened")]


async def test_each_group_is_a_node_covering_the_chunk_to_its_end(
    chunk_timestamps: TimestampPolicy,
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    chunk_seams: dict,
) -> None:
    # meiva:note
    # Request [head, m0..m3]; chunk [1, 5): four units in three groups.
    chunk_seams["answer"] = lambda located: chunk_result(
        located,
        UnitGroup(0, 0, "first"),
        UnitGroup(1, 2, "second"),
        UnitGroup(3, 3, "third"),
        timeline_inputs=chunk_timestamps,
    )
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 5), end_msg_id="m3")
    await run_rounds(aops_pool, 1, model_catalog=model_catalog)
    assert await written_nodes(aops_pool) == [
        (5, 1, 0, 0, "first"),
        (5, 1, 1, 2, "second"),
        (5, 1, 3, 3, "third"),
    ]
    assert await job_spans(aops_pool) == [("done", 1, 5)]
    assert set(chunk_seams["kw"]) == {"config"}
    assert isinstance(chunk_seams["kw"]["config"], ConfigBoot)


async def test_each_chunk_starts_at_its_own_start_whatever_the_previous_one_did(
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, chunk_seams: dict
) -> None:
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(3, 5), end_msg_id="m3")
    await run_rounds(aops_pool, 2, model_catalog=model_catalog)
    assert [[m.id for m in loc.messages] for loc in chunk_seams["described"]] == [
        ["m0", "m1"],
        ["m2", "m3"],
    ]
    assert await written_nodes(aops_pool) == [
        (5, 1, 0, 1, "what happened"),
        (5, 1, 2, 3, "what happened"),
    ]


async def test_a_refused_grouping_fails_the_job_at_once_without_nodes(
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(*_a: object, **_k: object) -> str:
        raise GenerateError("grouping reply refused after 2 correction(s): no <groups>")

    monkeypatch.setattr(loop, "describe", refused)
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await run_rounds(aops_pool, 1, model_catalog=model_catalog)
    [(status, attempts, error)] = await job_statuses(aops_pool)
    assert (status, attempts) == ("failed", 1) and "refused" in error
    assert await written_nodes(aops_pool) == []


async def test_a_done_job_is_followed_by_the_upper_level_checks(
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[int] = []

    async def standalone(
        _pool: object, _db: object, _models: object, agent_id: int, **_kw: object
    ) -> None:
        asked.append(agent_id)

    monkeypatch.setattr(loop, "run_group_checks", standalone)
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await run_rounds(aops_pool, 1, model_catalog=model_catalog)
    assert asked == [5]


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
    chunk_seams: dict,
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

    chunk_seams["answer"] = answer
    # meiva:note
    # agent 1: a1 then a2
    await _enqueue_ends(aops_pool, 1, "m1", "m3")
    # meiva:note
    # agent 2: b
    await _enqueue_ends(aops_pool, 2, "m2")
    await loop.Consumer(
        aops_pool, MagicMock(), [], **lm_wiring(model_catalog), config=consumer_config()
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
    chunk_seams: dict,
) -> None:
    barrier = threading.Barrier(5, timeout=10)

    def answer(located: LocatedChunk) -> ChunkResult:
        # meiva:note
        # passes only when all five jobs are inside their calls together
        barrier.wait()
        return chunk_result(located, timeline_inputs=chunk_timestamps)

    chunk_seams["answer"] = answer
    for agent in range(1, 6):
        await _enqueue_ends(aops_pool, agent, "m1")
    consumer = loop.Consumer(
        aops_pool, MagicMock(), [], **lm_wiring(model_catalog), config=consumer_config()
    )
    await consumer.run_until_idle()
    jobs = await job_statuses(aops_pool)
    assert [s for s, *_ in jobs] == ["done"] * 5, jobs
    assert consumer.in_flight == 0


async def test_one_jobs_failure_touches_no_other_job(
    chunk_timestamps: TimestampPolicy,
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    chunk_seams: dict,
) -> None:
    def answer(located: LocatedChunk) -> ChunkResult:
        if located.messages[-1].id == "m2":
            # meiva:note
            # not a GenerateError: the crash path
            raise RuntimeError("bug in one job")
        return chunk_result(located, timeline_inputs=chunk_timestamps)

    chunk_seams["answer"] = answer
    for agent, end in ((1, "m1"), (2, "m2"), (3, "m1")):
        await _enqueue_ends(aops_pool, agent, end)
    await loop.Consumer(
        aops_pool, MagicMock(), [], **lm_wiring(model_catalog), config=consumer_config()
    ).run_until_idle()
    status = [(s, e) for s, _, e in await job_statuses(aops_pool)]
    assert [s for s, _ in status] == ["done", "failed", "done"]
    assert "bug in one job" in str(status[1][1])


async def test_a_job_that_cannot_be_settled_is_left_to_its_lease_and_others_finish(
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = loop.settle_chunk_job

    async def settle(pool: AsyncConnectionPool, job: ChunkJob, outcome: loop.Outcome) -> None:
        if job.agent_id == 1:
            raise ConnectionError("database went away")
        await real(pool, job, outcome)

    monkeypatch.setattr(loop, "settle_chunk_job", settle)
    for agent in (1, 2):
        await _enqueue_ends(aops_pool, agent, "m1")
    consumer = loop.Consumer(
        aops_pool, MagicMock(), [], **lm_wiring(model_catalog), config=consumer_config()
    )
    # meiva:note
    # does not raise
    await consumer.run_until_idle()
    assert [s for s, *_ in await job_statuses(aops_pool)] == ["running", "done"]
    assert consumer.in_flight == 0


async def test_the_backlog_event_carries_in_flight(
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, emitted_events: EmittedEvents
) -> None:
    await _enqueue_ends(aops_pool, 1, "m1")
    consumer = loop.Consumer(
        aops_pool, MagicMock(), [], **lm_wiring(model_catalog), config=consumer_config()
    )
    consumer.in_flight = 1
    await consumer.emit_backlog()
    assert [event.attributes for event in emitted_events][-1]["pending"] == 1 and [
        event.attributes for event in emitted_events
    ][-1]["in_flight"] == 1


async def test_the_forever_loop_works_the_queue_and_stops_cleanly_on_cancel(
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.agent, "understanding_enabled", True)
    monkeypatch.setattr(loop, "POLL_SECONDS", 0.05)
    for agent in (1, 2, 3):
        await _enqueue_ends(aops_pool, agent, "m1")
    task = asyncio.create_task(
        loop.understanding_loop_forever(
            aops_pool, MagicMock(), [], **lm_wiring(model_catalog), config=consumer_config()
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
    chunk_seams: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[int] = []

    async def standalone(*_a: object, **_k: object) -> None:
        asked.append(1)

    monkeypatch.setattr(loop, "run_group_checks", standalone)
    # meiva:note
    # passes only when both segments are in flight
    both = threading.Barrier(2, timeout=10)

    def answer(located: LocatedChunk) -> ChunkResult:
        both.wait()
        return chunk_result(located, timeline_inputs=chunk_timestamps)

    chunk_seams["answer"] = answer
    for segment in (0, 1):
        await enqueue_chunk(
            aops_pool, 1, compact_version=segment, chunk=Chunk(1, 3), end_msg_id="m1"
        )
    await enqueue_chunk(aops_pool, 2, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await loop.replay_jobs(
        aops_pool, MagicMock(), [], 1, **lm_wiring(model_catalog), config=consumer_config()
    )
    assert [s for s, *_ in await job_statuses(aops_pool)] == ["done", "done", "pending"]
    assert asked == []


async def test_a_chunk_of_only_framework_notes_is_skipped_without_a_call(
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, chunk_seams: dict
) -> None:
    chunk_seams["history"] = notes_history()
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await run_rounds(aops_pool, 1, model_catalog=model_catalog)
    [(status, _, error)] = await job_statuses(aops_pool)
    assert status == "skipped" and "only framework notes" in error
    assert chunk_seams["described"] == [] and await written_nodes(aops_pool) == []
