# meiva:note
# These existing fault tests use heterogeneous spy state at precise failure boundaries.
# pyright: reportUnknownMemberType = warning
# pyright: reportUnknownArgumentType = warning
# pyright: reportUnknownLambdaType = warning
# pyright: reportUnknownVariableType = warning
# meiva:note
# The upper-level rebuild: its queue (claim order, who blocks whom) and the replay.
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from psycopg_pool import AsyncConnectionPool

from base.agents.history.hierarchy.chunks import (
    Chunk,
    ChunkJob,
    GroupNode,
    claim_job,
    enqueue_chunk,
    finish_job,
    write_group_nodes,
)
from base.agents.history.hierarchy.group import Group
from base.agents.history.hierarchy.rebuild import (
    claim_rebuild,
    finish_rebuild,
    rebuild_pending,
    release_rebuild,
)
from base.clock.tests.fakes import system_clock
from base.config import ConfigBoot
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.lm.catalog import ModelCatalog
from base.packages.tests.model_catalog import inert_keys
from base.telemetry.tests.capture.event_capture import EmittedEvents
from services.agent_runner.understanding import chunk_consumer as loop
from services.agent_runner.understanding import group_consumer as gc
from services.agent_runner.understanding.rebuild import run_rebuild
from services.agent_runner.understanding.tests.consumer_helpers import (
    GroupModels,
    consumer_config,
)

AGENT = 7


async def _rows(pool: AsyncConnectionPool, sql: str, *params: object) -> list[tuple[Any, ...]]:
    # meiva:note
    # These fixture statements use psycopg parameter binding and return the stored row tuples.
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, params)  # pyright: ignore[reportArgumentType]
        return await cur.fetchall() if cur.description else []


async def _leaf(
    pool: AsyncConnectionPool, i: int, *, depth: int = 1, engine: str = "chunk-0.2"
) -> int:
    start = datetime(2026, 10, 5, tzinfo=UTC) + timedelta(hours=i)
    rows = await _rows(
        pool,
        "INSERT INTO understanding_nodes (agent_id, depth, span_start, span_end, start_ts, end_ts,"
        " segment_key, text, text_hash, input_hash, children_count, model, engine_version,"
        " prompt_version, schema_version)"
        " VALUES (%s, %s, %s, %s, %s, %s, 'k', %s, 'h', 'i', 0, 'm', %s, 'p', 1) RETURNING id",
        AGENT,
        depth,
        i * 10,
        i * 10 + 9,
        start,
        start + timedelta(minutes=30),
        f"leaf {i}" if depth == 1 else f"stale {i}",
        engine,
    )
    return int(rows[0][0])


async def _enqueue_rebuild(pool: AsyncConnectionPool, agent: int = AGENT) -> int:
    rows = await _rows(
        pool, "INSERT INTO understanding_rebuilds (agent_id) VALUES (%s) RETURNING id", agent
    )
    return int(rows[0][0])


@pytest.fixture
def config() -> ConfigBoot:
    return consumer_config()


@pytest.fixture
def _groups(config: ConfigBoot, model_catalog: ModelCatalog) -> dict[str, Any]:
    # meiva:note
    # Grouping checks of five open nodes (no decay), closing the first three; each ask is recorded.
    config.set_field("understanding_group_check_open", 5)
    config.set_field("understanding_group_check_decay", 1)
    config.set_field("understanding_group_model", "deepseek-flash")
    config.set_field("understanding_group_corrections", 0)
    state: dict[str, Any] = {
        "asked": [],
        "reply": lambda nodes: [Group(nodes[0][0], nodes[2][0], f"group of {nodes[0][0]}")],
    }
    state["models"] = GroupModels(model_catalog, state)
    return state


async def _tree(pool: AsyncConnectionPool) -> list[tuple[Any, ...]]:
    return await _rows(
        pool,
        "SELECT depth, span_start, span_end, children_count FROM understanding_nodes"
        " WHERE agent_id = %s AND depth > 1 ORDER BY depth, span_start",
        AGENT,
    )


async def test_the_rebuild_replays_leaves_in_order_and_replaces_the_old_upper_levels(
    aops_pool: AsyncConnectionPool, _groups: dict[str, Any], *, config: ConfigBoot
) -> None:
    leaves = [await _leaf(aops_pool, i) for i in range(12)]
    stale = await _leaf(aops_pool, 50, depth=2)
    await _rows(
        aops_pool,
        "INSERT INTO understanding_group_state (agent_id, level, last_checked_open) VALUES (%s, 1, 99)",
        AGENT,
    )
    await _rows(
        aops_pool, "UPDATE understanding_nodes SET parent_id = %s WHERE id = %s", stale, leaves[0]
    )

    assert (
        await run_rebuild(
            aops_pool,
            MagicMock(),
            _groups["models"],
            AGENT,
            clock=system_clock(),
            config=config,
            min_check_open=1,
        )
        == 12
    )

    # meiva:note
    # Each check saw only the leaves that had "landed" by then (the replay horizon), though all
    # twelve exist: the first five; then, with two left open and five more arrived, seven.
    assert _groups["asked"] == [leaves[:5], leaves[3:10]]
    # meiva:note
    # Non-overlapping parents whose range is their children's: three leaves each.
    assert await _tree(aops_pool) == [(2, 0, 29, 3), (2, 30, 59, 3)]
    assert await _rows(aops_pool, "SELECT 1 FROM understanding_nodes WHERE id = %s", stale) == []
    children = await _rows(
        aops_pool,
        "SELECT p.span_start, p.span_end, min(c.span_start), max(c.span_end), count(*)"
        " FROM understanding_nodes p JOIN understanding_nodes c ON c.parent_id = p.id"
        " WHERE p.agent_id = %s GROUP BY p.id ORDER BY p.span_start",
        AGENT,
    )
    assert [(a, b, c, d) for a, b, c, d, _ in children] == [
        (0, 29, 0, 29),
        (30, 59, 30, 59),
    ]


async def test_rebuilding_twice_gives_the_same_tree(
    aops_pool: AsyncConnectionPool, _groups: dict[str, Any], *, config: ConfigBoot
) -> None:
    for i in range(12):
        await _leaf(aops_pool, i)
    await run_rebuild(
        aops_pool,
        MagicMock(),
        _groups["models"],
        AGENT,
        clock=system_clock(),
        config=config,
        min_check_open=1,
    )
    once = await _tree(aops_pool)
    await run_rebuild(
        aops_pool,
        MagicMock(),
        _groups["models"],
        AGENT,
        clock=system_clock(),
        config=config,
        min_check_open=1,
    )
    assert await _tree(aops_pool) == once and len(once) == 2


async def test_a_rebuild_touches_only_its_own_agent(
    aops_pool: AsyncConnectionPool, _groups: dict[str, Any], *, config: ConfigBoot
) -> None:
    for i in range(6):
        await _leaf(aops_pool, i)
    other = await _rows(
        aops_pool,
        "INSERT INTO understanding_nodes (agent_id, depth, span_start, span_end, start_ts, end_ts,"
        " segment_key, text, text_hash, input_hash, children_count, model, engine_version,"
        " prompt_version, schema_version) VALUES (8, 2, 0, 5, now(), now(), 'k', 't', 'h', 'i', 0,"
        " 'm', 'group-0.1', 'p', 1) RETURNING id",
    )
    await run_rebuild(
        aops_pool,
        MagicMock(),
        _groups["models"],
        AGENT,
        clock=system_clock(),
        config=config,
        min_check_open=1,
    )
    assert await _rows(aops_pool, "SELECT 1 FROM understanding_nodes WHERE id = %s", other[0][0])


async def test_the_claim_waits_for_the_agents_chunk_jobs_and_chunk_jobs_wait_for_a_running_rebuild(
    aops_pool: AsyncConnectionPool,
) -> None:
    await enqueue_chunk(aops_pool, AGENT, compact_version=0, chunk=Chunk(0, 10), end_msg_id="m10")
    rebuild_id = await _enqueue_rebuild(aops_pool)
    assert await rebuild_pending(aops_pool, AGENT)
    # meiva:note
    # a chunk job of the agent is live
    assert await claim_rebuild(aops_pool) is None
    job = await claim_job(aops_pool)
    assert job is not None
    # meiva:note
    # running counts as live
    assert await claim_rebuild(aops_pool) is None
    await finish_job(aops_pool, job.id, status="done")

    claimed = await claim_rebuild(aops_pool)
    assert claimed is not None and (claimed.id, claimed.agent_id, claimed.attempts) == (
        rebuild_id,
        AGENT,
        1,
    )
    assert not await rebuild_pending(aops_pool, AGENT)
    # meiva:note
    # While it runs, the agent's next chunk job is held back; another agent's is not.
    await enqueue_chunk(aops_pool, AGENT, compact_version=0, chunk=Chunk(10, 20), end_msg_id="m20")
    assert await claim_job(aops_pool) is None
    await enqueue_chunk(aops_pool, 8, compact_version=0, chunk=Chunk(0, 5), end_msg_id="x5")
    other = await claim_job(aops_pool)
    assert other is not None and other.agent_id == 8
    await finish_rebuild(aops_pool, claimed.id, status="done", leaves=4)
    held = await claim_job(aops_pool)
    assert held is not None and held.agent_id == AGENT


async def test_a_rebuild_handed_back_waits_out_the_spacing_and_one_rebuild_runs_per_agent(
    aops_pool: AsyncConnectionPool,
) -> None:
    first = await _enqueue_rebuild(aops_pool)
    # meiva:note
    # a build that found the first already running
    second = await _enqueue_rebuild(aops_pool)
    claimed = await claim_rebuild(aops_pool)
    assert claimed is not None and claimed.id == first
    # meiva:note
    # the agent has one running
    assert await claim_rebuild(aops_pool) is None
    await release_rebuild(aops_pool, first, error="boom")
    # meiva:note
    # the handed-back one waits out the spacing
    again = await claim_rebuild(aops_pool)
    assert again is not None and again.id == second
    await finish_rebuild(aops_pool, second, status="done")
    assert await claim_rebuild(aops_pool) is None
    await _rows(
        aops_pool,
        "UPDATE understanding_rebuilds SET claimed_at = now() - interval '1 hour' WHERE id = %s",
        first,
    )
    retaken = await claim_rebuild(aops_pool)
    assert retaken is not None and (retaken.id, retaken.attempts) == (first, 2)


async def test_the_consumer_runs_a_claimed_rebuild_and_settles_it(
    aops_pool: AsyncConnectionPool,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
) -> None:
    for i in range(7):
        await _leaf(aops_pool, i)
    rebuild_id = await _enqueue_rebuild(aops_pool)
    await loop.Consumer(
        aops_pool,
        MagicMock(),
        [],
        clock=system_clock(),
        catalog=model_catalog,
        keys=inert_keys(model_catalog),
        llm_override=config_authority.service_field_value("llm_override"),
        config=consumer_config(),
    ).run_until_idle()
    assert await _rows(
        aops_pool, "SELECT status, leaves FROM understanding_rebuilds WHERE id = %s", rebuild_id
    ) == [("done", 7)]


async def test_a_failing_rebuild_is_retried_then_failed(
    aops_pool: AsyncConnectionPool,
    emitted_events: EmittedEvents,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
) -> None:
    async def broken(*_a: object, **_kw: object) -> int:
        raise RuntimeError("provider down")

    rebuild_id = await _enqueue_rebuild(aops_pool)
    consumer = loop.Consumer(
        aops_pool,
        MagicMock(),
        [],
        clock=system_clock(),
        catalog=model_catalog,
        keys=inert_keys(model_catalog),
        llm_override=config_authority.service_field_value("llm_override"),
        config=consumer_config(),
        run_rebuild=broken,
    )
    for attempt in (1, 2, 3):
        await _rows(
            aops_pool,
            "UPDATE understanding_rebuilds SET claimed_at = now() - interval '1 hour' WHERE id = %s",
            rebuild_id,
        )
        await consumer.run_until_idle()
        status = (
            await _rows(
                aops_pool,
                "SELECT status, attempts, error FROM understanding_rebuilds WHERE id = %s",
                rebuild_id,
            )
        )[0]
        assert status[1] == attempt
    assert status[0] == "failed" and "provider down" in status[2]


async def _land(pool: AsyncConnectionPool, i: int) -> None:
    # meiva:note
    # Land leaf `i` the way a chunk job does (same spans as `_leaf`).
    at = datetime(2026, 10, 5, tzinfo=UTC) + timedelta(hours=i)
    job = ChunkJob(1, AGENT, 0, i * 10, i * 10 + 9, "m", None, 1)
    node = GroupNode((i * 10, i * 10 + 9), at, at + timedelta(minutes=30), f"leaf {i}")
    await write_group_nodes(pool, job, [node], model="m")


async def _open_before_parented(pool: AsyncConnectionPool) -> list[tuple[Any, ...]]:
    return await _rows(
        pool,
        "SELECT o.id FROM understanding_nodes o WHERE o.agent_id = %s AND o.depth = 1"
        " AND o.parent_id IS NULL AND EXISTS (SELECT 1 FROM understanding_nodes p"
        " WHERE p.agent_id = o.agent_id AND p.depth = 1 AND p.parent_id IS NOT NULL"
        " AND p.span_start > o.span_end)",
        AGENT,
    )


async def test_a_leaf_landing_before_a_grouped_one_is_reported_and_only_a_queued_rebuild_closes_it(
    aops_pool: AsyncConnectionPool,
    _groups: dict[str, Any],
    emitted_events: EmittedEvents,
    *,
    config: ConfigBoot,
) -> None:
    # meiva:note
    # The later segment lands first and is grouped; the earlier one arrives afterwards.
    for i in range(5, 10):
        await _land(aops_pool, i)
    await gc.run_group_checks(
        aops_pool,
        MagicMock(),
        _groups["models"],
        AGENT,
        clock=system_clock(),
        config=config,
        min_check_open=1,
    )
    # meiva:note
    # in order so far
    assert emitted_events.names() == []

    for i in range(5):
        await _land(aops_pool, i)
    # meiva:note
    # Reported, and nothing is queued: the gap stays until a person builds.
    assert emitted_events.names() and set(emitted_events.names()) == {
        "understanding_leaf_out_of_order"
    }
    assert not await rebuild_pending(aops_pool, AGENT)
    assert await _rows(aops_pool, "SELECT 1 FROM understanding_rebuilds") == []
    assert await _open_before_parented(aops_pool)

    await _rows(aops_pool, "INSERT INTO understanding_rebuilds (agent_id) VALUES (%s)", AGENT)
    await run_rebuild(
        aops_pool,
        MagicMock(),
        _groups["models"],
        AGENT,
        clock=system_clock(),
        config=config,
        min_check_open=1,
    )
    assert await _open_before_parented(aops_pool) == []


async def test_leaves_landing_in_order_queue_no_rebuild(
    aops_pool: AsyncConnectionPool,
    _groups: dict[str, Any],
    emitted_events: EmittedEvents,
    *,
    config: ConfigBoot,
) -> None:
    for i in range(10):
        await _land(aops_pool, i)
        await gc.run_group_checks(
            aops_pool,
            MagicMock(),
            _groups["models"],
            AGENT,
            clock=system_clock(),
            config=config,
            min_check_open=1,
        )
    assert await _rows(aops_pool, "SELECT 1 FROM understanding_rebuilds") == []
    # meiva:note
    # a rewrite of an existing, grouped span is not out of order
    await _land(aops_pool, 9)
    assert await _rows(aops_pool, "SELECT 1 FROM understanding_rebuilds") == []
    assert emitted_events.names() == []


async def test_each_rebuild_horizon_observes_the_same_owners_current_cadence(
    aops_pool: AsyncConnectionPool,
    database: Database,
    model_catalog: ModelCatalog,
    emitted_events: EmittedEvents,
) -> None:
    import asyncio
    from dataclasses import replace

    from base.lm.provider_api import ProviderBinding
    from services.agent_runner.understanding.tests.provider_wire import (
        ProviderWire,
        completion,
    )

    _ = emitted_events
    for i in range(12):
        await _leaf(aops_pool, i)
    config = consumer_config()
    config.set_field("understanding_group_model", "deepseek-flash")
    config.set_field("understanding_group_reasoning", "high")
    config.set_field("understanding_group_corrections", 0)
    config.set_field("understanding_group_check_open", 6)
    config.set_field("understanding_group_check_decay", 1)
    with ProviderWire(200, completion("no groups")) as wire:
        wire.release.clear()
        binding = ProviderBinding(
            prefix="deepseek-",
            display_name="Local SDK transport",
            key_env="DEEPSEEK_API_KEY",
            build=wire.build,
            effort_levels=("high", "max"),
            provider_key="openai",
        )
        catalog = replace(model_catalog, bindings={**model_catalog.bindings, "deepseek-": binding})
        models = loop.ModelCache(catalog, inert_keys(catalog), "")
        async with asyncio.TaskGroup() as tasks:
            rebuild = tasks.create_task(
                run_rebuild(aops_pool, database, models, AGENT, clock=system_clock(), config=config)
            )
            assert await asyncio.to_thread(wire.arrived.wait, 5)
            config.set_field("understanding_group_check_open", 60)
            wire.release.set()
            assert await rebuild == 12
        assert len(wire.requests) == 1
        assert await _rows(
            aops_pool, "SELECT last_checked_open, claimed_at FROM understanding_group_state"
        ) == [(6, None)]
        assert await _rows(aops_pool, "SELECT count(*) FROM understanding_group_calls") == [(1,)]
