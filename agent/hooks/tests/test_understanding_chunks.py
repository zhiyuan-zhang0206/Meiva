from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from psycopg_pool import AsyncConnectionPool

from agent.hooks.snapshot_wait import SnapshotWait
from agent.hooks.understanding_chunks import await_snapshot, due_chunk_update, enqueue_closing_chunk
from agent.state import AgentState
from agent.state_channels import CompactState
from base.agents.history.hierarchy.chunks import chunk_threshold
from base.clock import SYSTEM_TIMERS
from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.telemetry.tests.capture.event_capture import EmittedEvents

_MODEL = "deepseek-flash"
# meiva:note
# A real model, a small ratio: the threshold is about 750 tokens.
_RATIO = 0.002
_AGENT = 3


def _agent(*, enabled: bool = True) -> AgentSlices:
    return AgentSlices.resolve(
        {
            "llm_model": _MODEL,
            "understanding_enabled": enabled,
            "understanding_chunk_ratio": _RATIO,
        },
        default_reader=defaults(),
    )


def _threshold(model_catalog: ModelCatalog) -> int:
    agent = _agent()
    return chunk_threshold(_MODEL, agent.overrides, _RATIO, catalog=model_catalog)


def _request(n: int) -> list[AnyMessage]:
    return [
        SystemMessage(content="head", id="h"),
        *(HumanMessage(content=str(i), id=f"m{i}") for i in range(1, n)),
    ]


def _reply(input_tokens: int) -> AIMessage:
    return AIMessage(
        content="x",
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": 1,
            "total_tokens": input_tokens + 1,
        },
    )


# A segment past its first turn: cut just after the head, 500 tokens of head.
_BASELINED = CompactState(understanding_cut_index=1, understanding_cut_tokens=500)


async def _jobs(pool: AsyncConnectionPool) -> list[tuple[Any, ...]]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT agent_id, compact_version, start_index, end_index, end_msg_id,"
            " boundary_checkpoint_id, status FROM understanding_chunk_jobs ORDER BY end_index"
        )
        return await cur.fetchall()


async def test_a_turn_past_the_threshold_queues_the_chunk_and_moves_the_cut(
    aops_pool: AsyncConnectionPool, model_catalog: ModelCatalog
) -> None:
    compact = _BASELINED.model_copy(update={"version": 4})

    update = await due_chunk_update(
        compact,
        _request(10),
        _reply(500 + _threshold(model_catalog)),
        pool=aops_pool,
        agent_id=_AGENT,
        agent=_agent(),
        catalog=model_catalog,
    )

    assert await _jobs(aops_pool) == [(_AGENT, 4, 1, 10, "m9", None, "pending")]
    moved = update["compact"]
    assert (moved.understanding_cut_index, moved.understanding_cut_tokens, moved.version) == (
        10,
        500 + _threshold(model_catalog),
        4,
    )


async def test_a_turn_below_the_threshold_queues_nothing(
    aops_pool: AsyncConnectionPool, model_catalog: ModelCatalog
) -> None:
    update = await due_chunk_update(
        _BASELINED,
        _request(10),
        _reply(500 + _threshold(model_catalog) - 1),
        pool=aops_pool,
        agent_id=_AGENT,
        agent=_agent(),
        catalog=model_catalog,
    )

    assert update == {}
    assert await _jobs(aops_pool) == []


async def test_a_segments_first_turn_records_the_baseline_without_queueing(
    aops_pool: AsyncConnectionPool, model_catalog: ModelCatalog
) -> None:
    update = await due_chunk_update(
        CompactState(),
        _request(2),
        _reply(30_000),
        pool=aops_pool,
        agent_id=_AGENT,
        agent=_agent(),
        catalog=model_catalog,
    )

    baseline = update["compact"]
    assert (baseline.understanding_cut_index, baseline.understanding_cut_tokens) == (1, 30_000)
    assert await _jobs(aops_pool) == []


# meiva:note
# A chunk whose last message has no id cannot be located in the checkpoint later, so the queue refuses it.
async def test_a_failed_enqueue_keeps_the_cut_so_the_stretch_is_retried(
    aops_pool: AsyncConnectionPool, model_catalog: ModelCatalog
) -> None:
    request = [*_request(9), HumanMessage(content="no id")]

    update = await due_chunk_update(
        _BASELINED,
        request,
        _reply(500 + _threshold(model_catalog)),
        pool=aops_pool,
        agent_id=_AGENT,
        agent=_agent(),
        catalog=model_catalog,
    )

    assert update == {}
    assert await _jobs(aops_pool) == []


async def test_a_turn_with_understanding_off_or_without_a_pool_does_nothing(
    aops_pool: AsyncConnectionPool, model_catalog: ModelCatalog
) -> None:
    past = _reply(500 + _threshold(model_catalog))

    poolless = await due_chunk_update(
        _BASELINED,
        _request(10),
        past,
        pool=None,
        agent_id=_AGENT,
        agent=_agent(),
        catalog=model_catalog,
    )
    disabled = await due_chunk_update(
        _BASELINED,
        _request(10),
        past,
        pool=aops_pool,
        agent_id=_AGENT,
        agent=_agent(enabled=False),
        catalog=model_catalog,
    )

    assert poolless == disabled == {}
    assert await _jobs(aops_pool) == []


async def test_compaction_queues_the_remainder_against_the_boundary(
    aops_pool: AsyncConnectionPool,
) -> None:
    compact = CompactState(version=2, understanding_cut_index=6, understanding_cut_tokens=900)

    await enqueue_closing_chunk(
        compact,
        _request(12),
        pool=aops_pool,
        agent_id=_AGENT,
        boundary="cp-9",
        understanding_enabled=True,
    )

    assert await _jobs(aops_pool) == [(_AGENT, 2, 6, 12, "m11", "cp-9", "pending")]


async def test_compaction_without_a_remainder_a_boundary_or_the_feature_queues_nothing(
    aops_pool: AsyncConnectionPool,
) -> None:
    at_end = CompactState(understanding_cut_index=12)
    for compact, boundary, enabled in (
        (at_end, "cp-9", True),
        (CompactState(), None, True),
        (CompactState(), "cp-9", False),
    ):
        await enqueue_closing_chunk(
            compact,
            _request(12),
            pool=aops_pool,
            agent_id=_AGENT,
            boundary=boundary,
            understanding_enabled=enabled,
        )
    await enqueue_closing_chunk(
        CompactState(),
        _request(12),
        pool=None,
        agent_id=_AGENT,
        boundary="cp-9",
        understanding_enabled=True,
    )

    assert await _jobs(aops_pool) == []


def _state(*messages: AnyMessage) -> AgentState:
    return AgentState(messages=[*_request(3), *messages], halted=False)


def _stamped(seconds: int) -> HumanMessage:
    return HumanMessage(
        content="m",
        id=f"s{seconds}",
        additional_kwargs={"meiva_created_at": f"2026-10-07T12:00:{seconds:02d}+00:00"},
    )


async def _checkpoint(pool: AsyncConnectionPool, checkpoint_id: str, seconds: int) -> None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, checkpoint,"
            " metadata) VALUES (%s, '', %s, 'x', %s::jsonb, '{}'::jsonb)",
            (
                str(_AGENT),
                checkpoint_id,
                json.dumps({"ts": f"2026-10-07T12:00:{seconds:02d}+00:00"}),
            ),
        )


_QUICK = SnapshotWait(wait_seconds=0.3, poll_seconds=0.01)


async def test_the_boundary_waits_for_a_checkpoint_as_new_as_the_states_last_message(
    aops_pool: AsyncConnectionPool, emitted_events: EmittedEvents
) -> None:
    await _checkpoint(aops_pool, "c1", 5)  # the last message is from second 10
    waiting = asyncio.create_task(
        await_snapshot(
            aops_pool,
            _state(_stamped(10)),
            _AGENT,
            understanding_enabled=True,
            wait=SnapshotWait(wait_seconds=30.0, poll_seconds=0.01),
            timers=SYSTEM_TIMERS,
        )
    )
    await asyncio.sleep(0.1)
    assert not waiting.done()

    await _checkpoint(aops_pool, "c2", 20)
    await asyncio.wait_for(waiting, timeout=10)

    assert emitted_events.names() == []


async def test_a_snapshot_that_never_catches_up_is_stamped_anyway_with_an_event(
    aops_pool: AsyncConnectionPool, emitted_events: EmittedEvents
) -> None:
    await _checkpoint(aops_pool, "c1", 5)

    await await_snapshot(
        aops_pool,
        _state(_stamped(10)),
        _AGENT,
        understanding_enabled=True,
        wait=_QUICK,
        timers=SYSTEM_TIMERS,
    )

    assert emitted_events.names() == ["understanding_snapshot_lag"]
    assert emitted_events.attributes("understanding_snapshot_lag")[0]["agent_id"] == _AGENT


# meiva:note
# Arrived at second 3, picked up at second 10: a checkpoint at second 5 predates the pickup, so the stamp does not pass (its arrival time alone would let it).
async def test_an_inbound_last_message_is_compared_by_the_time_the_model_read_it(
    aops_pool: AsyncConnectionPool, emitted_events: EmittedEvents
) -> None:
    await _checkpoint(aops_pool, "c1", 5)
    inbound = HumanMessage(
        content="in",
        id="in1",
        additional_kwargs={
            "meiva_msg_type": "inbound",
            "meiva_created_at": "2026-10-07T12:00:03+00:00",
            "meiva_picked_up_at": "2026-10-07T12:00:10+00:00",
        },
    )

    await await_snapshot(
        aops_pool,
        _state(inbound),
        _AGENT,
        understanding_enabled=True,
        wait=_QUICK,
        timers=SYSTEM_TIMERS,
    )

    assert emitted_events.names() == ["understanding_snapshot_lag"]


# meiva:note
# Old messages have no timezone (or no stamp at all): the check cannot pass, so it is the timeout path at once: no exception, the boundary is stamped, the lag event says so. A checkpoint from far in the future is there to be read, and is not.
@pytest.mark.parametrize(
    "stamp",
    [None, "2026-10-07T12:00:10", "not a time"],
    ids=["no timestamp", "no timezone", "unparsable"],
)
async def test_a_last_message_without_a_usable_time_is_not_waved_through_or_an_error(
    aops_pool: AsyncConnectionPool, stamp: str | None, emitted_events: EmittedEvents
) -> None:
    await _checkpoint(aops_pool, "c1", 59)
    kwargs = {} if stamp is None else {"meiva_created_at": stamp}
    last = HumanMessage(content="old", id="old", additional_kwargs=kwargs)

    await await_snapshot(
        aops_pool,
        _state(last),
        _AGENT,
        understanding_enabled=True,
        wait=SnapshotWait(wait_seconds=30.0, poll_seconds=0.01),
        timers=SYSTEM_TIMERS,
    )

    assert emitted_events.names() == ["understanding_snapshot_lag"]


async def test_nothing_is_waited_for_when_understanding_is_off_or_there_is_nothing_to_compare(
    aops_pool: AsyncConnectionPool, emitted_events: EmittedEvents
) -> None:
    await await_snapshot(
        aops_pool,
        _state(_stamped(10)),
        _AGENT,
        understanding_enabled=False,
        wait=_QUICK,
        timers=SYSTEM_TIMERS,
    )
    await await_snapshot(
        aops_pool, None, _AGENT, understanding_enabled=True, wait=_QUICK, timers=SYSTEM_TIMERS
    )
    await await_snapshot(
        None,
        _state(_stamped(10)),
        _AGENT,
        understanding_enabled=True,
        wait=_QUICK,
        timers=SYSTEM_TIMERS,
    )

    assert emitted_events.names() == []
