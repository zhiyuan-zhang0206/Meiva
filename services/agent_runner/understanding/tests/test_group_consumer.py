# meiva:note
# These existing fault tests use heterogeneous spy state at precise failure boundaries.
# pyright: reportUnknownMemberType = warning
# pyright: reportUnknownArgumentType = warning
# pyright: reportUnknownLambdaType = warning
# pyright: reportUnknownVariableType = warning
# meiva:note
# Upper-level grouping run by the consumer: due checks, sealing, the durable cursor, failures.

from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage
from psycopg_pool import AsyncConnectionPool

from base.agents.history.hierarchy.generate import GenerateError
from base.agents.history.hierarchy.group import Group, GroupCall, OpenNode, parse_groups
from base.agents.history.hierarchy.group_store import (
    GroupOrderError,
    claim_check,
    load_last_checked,
    load_open_nodes,
    write_groups,
)
from base.config import ConfigBoot
from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog
from base.telemetry.tests.capture.event_capture import EmittedEvents
from services.agent_runner.understanding import group_consumer as gc
from services.agent_runner.understanding.tests.consumer_helpers import consumer_config

AGENT = 7


@pytest.fixture
def config() -> ConfigBoot:
    return consumer_config()


def test_generation_reads_late_policy_after_model_construction(
    config: ConfigBoot, model_catalog: ModelCatalog
) -> None:
    # meiva:note
    # The real SDK is built before corrections, rendering and the close brake are captured.
    import json
    from dataclasses import replace

    from base.lm.provider_api import BuildContext, ProviderBinding
    from base.packages.tests.model_catalog import inert_keys
    from services.agent_runner.understanding.chunk_consumer import ModelCache
    from services.agent_runner.understanding.tests.provider_wire import (
        ProviderWire,
        completion,
    )

    config.set_field("understanding_group_reasoning", "high")
    config.set_field("understanding_group_corrections", 2)
    config.set_field("understanding_group_check_open", 60)
    config.set_field("timezone", "UTC")
    at = datetime(2026, 10, 5, tzinfo=UTC)
    nodes = [
        OpenNode(i, i * 10, i * 10 + 9, at, at + timedelta(minutes=30), "leaf") for i in range(18)
    ]
    calls: list[GroupCall] = []
    with ProviderWire(200, completion("no groups")) as wire:

        def build(ctx: BuildContext):
            model = wire.build(ctx)
            # meiva:note
            # This explicit test-owner update is not production hot reload.
            config.set_field("understanding_group_reasoning", "off")
            config.set_field("understanding_group_corrections", 0)
            config.set_field("timezone", "Europe/London")
            config.set_field("understanding_group_check_open", 6)
            config.set_field("understanding_group_check_decay", 1)
            return model

        binding = ProviderBinding(
            prefix="deepseek-",
            display_name="Local SDK transport",
            key_env="DEEPSEEK_API_KEY",
            build=build,
            effort_levels=("high", "max"),
            provider_key="openai",
        )
        catalog = replace(model_catalog, bindings={**model_catalog.bindings, "deepseek-": binding})
        models = ModelCache(catalog, inert_keys(catalog), "")
        with pytest.raises(GenerateError, match="after 0 correction"):
            gc.generate(
                models,
                "deepseek-flash",
                ModelOverrides.from_pins(None),
                1,
                nodes,
                calls,
                7,
                config=config,
            )
        [request] = [json.loads(body) for body in wire.requests]
        assert wire.build_efforts == ["high"]
        assert request["reasoning_effort"] == "high"
        prompt = request["messages"][0]["content"]
        assert "at least one group must be closed" in prompt
        assert "[2026-10-05 01:00:00]" in prompt
        assert len(calls) == 1 and calls[0].round == 0
        assert calls[0].problem is not None and "at least one group" in calls[0].problem


def _first(row: tuple | None) -> int:
    assert row is not None
    return int(row[0])


async def _add_leaf(pool: AsyncConnectionPool, i: int, depth: int = 1, agent: int = AGENT) -> int:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO understanding_nodes (agent_id, depth, span_start, span_end, start_ts, end_ts,"
            " segment_key, text, text_hash, input_hash, children_count, model, engine_version,"
            " prompt_version, schema_version)"
            " VALUES (%s, %s, %s, %s, %s, %s, 'k', %s, 'h', 'i', 0, 'm', 'chunk-0.2', 'p', 1) RETURNING id",
            (
                agent,
                depth,
                i * 10,
                i * 10 + 9,
                datetime(2026, 10, 5, tzinfo=UTC) + timedelta(hours=i),
                datetime(2026, 10, 5, tzinfo=UTC) + timedelta(hours=i, minutes=30),
                f"leaf {i}",
            ),
        )
        row = await cur.fetchone()
    return _first(row)


async def _fill(pool: AsyncConnectionPool, count: int, start: int = 0) -> list[int]:
    return [await _add_leaf(pool, i) for i in range(start, start + count)]


@pytest.fixture(autouse=True)
def _small_step(config: ConfigBoot, monkeypatch: pytest.MonkeyPatch) -> None:
    """The cadence is configurable (default 60); these tests drive it with five."""
    config.set_field("understanding_group_check_open", 5)
    config.set_field("understanding_group_check_decay", 1)
    # meiva:note
    # no floor, no decay: five at every level
    monkeypatch.setattr(gc, "MIN_CHECK_OPEN", 1)


@pytest.fixture
def _seams(monkeypatch: pytest.MonkeyPatch, emitted_events: EmittedEvents) -> dict:
    # meiva:note
    # the consumer's events go to the capture pipeline, not to a sink failure
    _ = emitted_events
    seen: dict = {"asked": [], "reply": lambda _nodes: []}
    monkeypatch.setattr(
        gc, "group_model", lambda *_a, **_k: ("deepseek-flash", ModelOverrides.from_pins(None))
    )

    def generate(
        _models: object,
        model: str,
        _o: object,
        _level: int,
        nodes: list[OpenNode],
        calls: list,
        _agent_id: int,
        **_kw: object,
    ) -> list[Group]:
        seen["asked"].append([n.id for n in nodes])
        calls.append(GroupCall(0, model, "prompt", AIMessage(content="r"), 5.0, None, None))
        return seen["reply"](nodes)

    monkeypatch.setattr(gc, "generate", generate)
    return seen


async def _parents(pool: AsyncConnectionPool) -> list[tuple]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT depth, span_start, span_end, children_count, text FROM understanding_nodes"
            " WHERE depth > 1 ORDER BY depth, span_start"
        )
        return await cur.fetchall()


def test_the_threshold_falls_per_level_down_to_a_floor(
    config: ConfigBoot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gc, "MIN_CHECK_OPEN", 6)
    config.set_field("understanding_group_check_open", 60)
    config.set_field("understanding_group_check_decay", 3)
    assert [gc.check_threshold(level, config=config) for level in (1, 2, 3, 4, 5, 9)] == [
        60,
        20,
        7,
        6,
        6,
        6,
    ]
    config.set_field("understanding_group_check_open", 90)
    config.set_field("understanding_group_check_decay", 3)
    assert [gc.check_threshold(level, config=config) for level in (1, 2, 3, 4)] == [
        90,
        30,
        10,
        6,
    ]
    config.set_field("understanding_group_check_decay", 1)
    assert [gc.check_threshold(level, config=config) for level in (1, 5)] == [90, 90]


async def test_the_cadence_setting_decides_when_a_level_is_due(
    config: ConfigBoot,
    aops_pool: AsyncConnectionPool,
    _seams: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config.set_field("understanding_group_check_open", 8)
    await _fill(aops_pool, 7)
    await gc.run_group_checks(aops_pool, MagicMock(), MagicMock(), AGENT, config=config)
    assert _seams["asked"] == []
    await _add_leaf(aops_pool, 7)
    await gc.run_group_checks(aops_pool, MagicMock(), MagicMock(), AGENT, config=config)
    assert len(_seams["asked"]) == 1


async def test_nothing_is_asked_below_five_open_nodes(
    config: ConfigBoot, aops_pool: AsyncConnectionPool, _seams: dict
) -> None:
    await _fill(aops_pool, 4)
    await gc.run_group_checks(aops_pool, MagicMock(), MagicMock(), AGENT, config=config)
    assert _seams["asked"] == []


async def test_a_check_closes_groups_links_children_and_resets_the_baseline(
    config: ConfigBoot, aops_pool: AsyncConnectionPool, _seams: dict
) -> None:
    ids = await _fill(aops_pool, 6)
    _seams["reply"] = lambda nodes: [Group(nodes[0].id, nodes[2].id, "first three")]
    await gc.run_group_checks(aops_pool, MagicMock(), MagicMock(), AGENT, config=config)
    assert _seams["asked"] == [ids]
    assert await _parents(aops_pool) == [(2, 0, 29, 3, "first three")]
    assert [n.id for n in await load_open_nodes(aops_pool, AGENT, 1)] == ids[3:]
    # meiva:note
    # what stays open
    assert await load_last_checked(aops_pool, AGENT, 1) == 3
    async with aops_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM understanding_group_calls WHERE agent_id = %s", (AGENT,)
        )
        assert _first(await cur.fetchone()) == 1


async def test_a_declined_check_is_not_repeated_at_the_same_count(
    config: ConfigBoot, aops_pool: AsyncConnectionPool, _seams: dict
) -> None:
    await _fill(aops_pool, 5)
    await gc.run_group_checks(aops_pool, MagicMock(), MagicMock(), AGENT, config=config)
    await gc.run_group_checks(aops_pool, MagicMock(), MagicMock(), AGENT, config=config)
    assert len(_seams["asked"]) == 1 and await load_last_checked(aops_pool, AGENT, 1) == 5
    # meiva:note
    # 9 open: still short of 10
    await _fill(aops_pool, 4, start=5)
    await gc.run_group_checks(aops_pool, MagicMock(), MagicMock(), AGENT, config=config)
    assert len(_seams["asked"]) == 1
    # meiva:note
    # 10 open
    await _fill(aops_pool, 1, start=9)
    await gc.run_group_checks(aops_pool, MagicMock(), MagicMock(), AGENT, config=config)
    assert len(_seams["asked"]) == 2


async def test_closing_a_level_can_make_the_next_level_due(
    config: ConfigBoot, aops_pool: AsyncConnectionPool, _seams: dict
) -> None:
    # meiva:note
    # five level-2 nodes already waiting
    for i in range(5):
        await _add_leaf(aops_pool, 100 + i, depth=2)
    await _fill(aops_pool, 5)
    _seams["reply"] = lambda nodes: (
        [Group(nodes[0].id, nodes[2].id, "g")] if nodes[0].text.startswith("leaf") else []
    )
    await gc.run_group_checks(aops_pool, MagicMock(), MagicMock(), AGENT, config=config)
    # meiva:note
    # level 1 closed one group; level 2 (now 6 open) was asked next
    assert len(_seams["asked"]) == 2
    assert await load_last_checked(aops_pool, AGENT, 2) == 6


async def test_a_failed_check_emits_an_event_records_calls_and_waits_for_five_more(
    config: ConfigBoot, aops_pool: AsyncConnectionPool, _seams: dict, emitted_events: EmittedEvents
) -> None:
    await _fill(aops_pool, 5)

    def boom(_nodes: list[OpenNode]) -> list[Group]:
        raise GenerateError("refused after 2 corrections")

    _seams["reply"] = boom
    await gc.run_group_checks(aops_pool, MagicMock(), MagicMock(), AGENT, config=config)
    assert emitted_events.names() == ["understanding_group_failed"]
    assert await load_last_checked(aops_pool, AGENT, 1) == 5
    assert await _parents(aops_pool) == []
    async with aops_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM understanding_group_calls")
        # meiva:note
        # the calls are recorded though the check failed
        assert _first(await cur.fetchone()) == 1
    await gc.run_group_checks(aops_pool, MagicMock(), MagicMock(), AGENT, config=config)
    # meiva:note
    # same count, not asked again
    assert len(_seams["asked"]) == 1


async def test_a_live_lease_blocks_a_second_checker(
    config: ConfigBoot, aops_pool: AsyncConnectionPool, _seams: dict
) -> None:
    await _fill(aops_pool, 5)
    assert await claim_check(aops_pool, AGENT, 1)
    await gc.run_group_checks(aops_pool, MagicMock(), MagicMock(), AGENT, config=config)
    assert _seams["asked"] == []
    async with aops_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE understanding_group_state SET claimed_at = now() - interval '1 hour'"
        )
    await gc.run_group_checks(aops_pool, MagicMock(), MagicMock(), AGENT, config=config)
    assert len(_seams["asked"]) == 1


async def test_a_crash_inside_a_check_frees_the_lease(
    config: ConfigBoot, aops_pool: AsyncConnectionPool, _seams: dict
) -> None:
    await _fill(aops_pool, 5)

    def crash(_nodes: list[OpenNode]) -> list[Group]:
        raise RuntimeError("bug")

    _seams["reply"] = crash
    # meiva:note
    # never raises
    await gc.run_group_checks(aops_pool, MagicMock(), MagicMock(), AGENT, config=config)
    # meiva:note
    # the lease was released
    assert await claim_check(aops_pool, AGENT, 1)


def test_the_configured_group_model_wins_over_the_agents(
    config: ConfigBoot, model_catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    config.set_field("understanding_group_model", "other-model")
    model, _overrides = gc.group_model(MagicMock(), 1, catalog=model_catalog, config=config)
    assert model == "other-model"


def test_without_a_configured_model_the_agents_own_is_used(
    config: ConfigBoot, model_catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    config.set_field("understanding_group_model", "")
    monkeypatch.setattr(
        gc, "agent_model_target", lambda *_a, **_k: ("agent-model", ModelOverrides.from_pins(None))
    )
    assert gc.group_model(MagicMock(), 1, catalog=model_catalog, config=config)[0] == "agent-model"


def test_standalone_groupings_hand_the_reasoning_setting_to_the_model(
    config: ConfigBoot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = MagicMock()
    captured: list[tuple] = []
    monkeypatch.setattr(gc, "generate_groups", lambda llm, *_a, **_k: captured.append((llm,)) or [])
    none = ModelOverrides.from_pins(None)
    for value in ("", "off", "high"):
        config.set_field("understanding_group_reasoning", value)
        gc.generate(models, "m", none, 1, [], [], 7, config=config)
        assert models.get.call_args.args == ("m", none, value)


# meiva:note
# -- Concurrency: leases serialize one (agent, level); different agents run together -----------


def _slow_generate(seen: dict, pause: float = 0.3):
    def generate(
        _models: object,
        model: str,
        _o: object,
        _level: int,
        nodes: list[OpenNode],
        calls: list,
        _agent_id: int,
        **_kw: object,
    ) -> list[Group]:
        seen["asked"].append((nodes[0].id, nodes[-1].id))
        time.sleep(pause)
        return []

    return generate


async def test_two_checkers_of_one_level_make_one_call(
    config: ConfigBoot,
    aops_pool: AsyncConnectionPool,
    _seams: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _fill(aops_pool, 5)
    monkeypatch.setattr(gc, "generate", _slow_generate(_seams))
    await asyncio.gather(
        *(
            gc.run_group_checks(aops_pool, MagicMock(), MagicMock(), AGENT, config=config)
            for _ in range(3)
        )
    )
    # meiva:note
    # the others found the lease held and moved on
    assert len(_seams["asked"]) == 1
    # meiva:note
    # and it was released
    assert await claim_check(aops_pool, AGENT, 1)


async def test_different_agents_are_checked_at_the_same_time(
    config: ConfigBoot,
    aops_pool: AsyncConnectionPool,
    _seams: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for agent in (7, 8):
        for i in range(5):
            await _add_leaf(aops_pool, i, agent=agent)
    # meiva:note
    # passes only if the two calls overlap
    both = threading.Barrier(2, timeout=10)

    def generate(
        _models: object,
        model: str,
        _o: object,
        _level: int,
        nodes: list[OpenNode],
        calls: list,
        _agent_id: int,
        **_kw: object,
    ) -> list[Group]:
        both.wait()
        return []

    monkeypatch.setattr(gc, "generate", generate)
    await asyncio.gather(
        *(
            gc.run_group_checks(aops_pool, MagicMock(), MagicMock(), a, config=config)
            for a in (7, 8)
        )
    )
    for agent in (7, 8):
        assert await load_last_checked(aops_pool, agent, 1) == 5


async def test_the_baseline_counts_leaves_that_landed_while_the_call_ran(
    aops_pool: AsyncConnectionPool,
) -> None:
    ids = await _fill(aops_pool, 5)
    nodes = await load_open_nodes(aops_pool, AGENT, 1)
    assert await claim_check(aops_pool, AGENT, 1)
    # meiva:note
    # the same agent's next job landed a leaf meanwhile
    await _add_leaf(aops_pool, 5)
    remaining = await write_groups(
        aops_pool, AGENT, 1, nodes, [Group(ids[0], ids[2], "g")], model="m", check_key="ck"
    )
    # meiva:note
    # two of the snapshot plus the newcomer
    assert remaining == 3
    assert await load_last_checked(aops_pool, AGENT, 1) == 3
    # meiva:note
    # released with the write
    assert await claim_check(aops_pool, AGENT, 1)


def test_an_open_set_past_three_checks_must_close_a_group(
    config: ConfigBoot, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[bool] = []
    monkeypatch.setattr(gc, "generate_groups", lambda *_a, **k: seen.append(k["must_close"]) or [])
    none = ModelOverrides.from_pins(None)
    nodes = [
        OpenNode(i, 0, 1, datetime(2026, 10, 5, tzinfo=UTC), datetime(2026, 10, 5, tzinfo=UTC), "t")
        for i in range(15)
    ]
    config.set_field("understanding_group_check_open", 5)
    gc.generate(MagicMock(), "m", none, 1, nodes[:14], [], 7, config=config)
    gc.generate(MagicMock(), "m", none, 1, nodes, [], 7, config=config)
    monkeypatch.setattr(gc, "MIN_CHECK_OPEN", 6)
    config.set_field("understanding_group_check_open", 60)
    config.set_field("understanding_group_check_decay", 3)
    # meiva:note
    # level 2: 3 x 20 = 60
    gc.generate(MagicMock(), "m", none, 2, nodes[:59], [], 7, config=config)
    gc.generate(MagicMock(), "m", none, 2, nodes * 4, [], 7, config=config)
    # meiva:note
    # 14 < 3 x 5, 15 = 3 x 5; per level: 59 < 60 = 3 x 20
    assert seen == [False, True, False, True]


async def _tree(pool: AsyncConnectionPool) -> tuple[list[tuple], list[tuple]]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT id, depth, span_start, span_end, parent_id FROM understanding_nodes"
            " WHERE agent_id = %s ORDER BY depth, span_start",
            (AGENT,),
        )
        rows = await cur.fetchall()
    return rows, [r for r in rows if r[1] > 1]


async def test_single_groups_in_the_middle_close_and_no_level_ever_overlaps(
    config: ConfigBoot,
    aops_pool: AsyncConnectionPool,
    _seams: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # meiva:note
    # Review repro: open nodes 1..6, a reply of `1..1`, `2..4`, `5..5`. Closing only the
    # middle group left nodes 1, 5, 6 open, and the next check grouped 1..5 into a parent that
    # contained the already closed 2..4. Now the head single closes, only the trailing single stays
    # open, and over many checks every level stays disjoint with each parent spanning exactly its
    # children.
    plans = [[(0, 0), (1, 3), (4, 4)], [(0, 1), (2, 2), (3, 5), (6, 6)], [(0, 0), (1, 1), (2, 4)]]
    state = {"n": 0}

    def generate(
        _models: object,
        model: str,
        _o: object,
        _level: int,
        nodes: list[OpenNode],
        calls: list,
        _agent_id: int,
        **_kw: object,
    ) -> list[Group]:
        plan = [(a, b) for a, b in plans[state["n"] % len(plans)] if b < len(nodes) - 1]
        state["n"] += 1
        text = "".join(
            f'<group first="{nodes[a].id}" last="{nodes[b].id}">g{nodes[a].id}</group>'
            for a, b in plan
        )
        calls.append(GroupCall(0, model, "prompt", AIMessage(content=text), 5.0, None, None))
        return parse_groups(text, nodes, must_close=False)

    monkeypatch.setattr(gc, "generate", generate)
    for i in range(60):
        await _add_leaf(aops_pool, i)
        await gc.run_group_checks(aops_pool, MagicMock(), MagicMock(), AGENT, config=config)
    rows, parents = await _tree(aops_pool)
    assert parents, "the plans must have closed some groups"
    by_parent: dict[int, list[tuple]] = {}
    for row in rows:
        if row[4] is not None:
            by_parent.setdefault(row[4], []).append(row)
    for depth in {r[1] for r in rows}:
        level = [r for r in rows if r[1] == depth]
        for left, right in pairwise(level):
            assert left[3] < right[2], f"level {depth}: {left} overlaps {right}"
    for parent in parents:
        kids = by_parent[parent[0]]
        assert (parent[2], parent[3]) == (min(k[2] for k in kids), max(k[3] for k in kids))


async def test_a_group_may_not_close_across_an_earlier_open_node(
    aops_pool: AsyncConnectionPool,
) -> None:
    # meiva:note
    # A leaf of an earlier span landed after the snapshot: closing the later group would seal it
    # inside the gap, so the write is refused and nothing changes.
    ids = await _fill(aops_pool, 5, start=5)
    nodes = await load_open_nodes(aops_pool, AGENT, 1)
    late = await _add_leaf(aops_pool, 0)
    assert await claim_check(aops_pool, AGENT, 1)
    with pytest.raises(GroupOrderError, match=f"open node {late}"):
        await write_groups(
            aops_pool, AGENT, 1, nodes, [Group(ids[0], ids[2], "g")], model="m", check_key="ck"
        )
    assert await _parents(aops_pool) == []
    async with aops_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM understanding_nodes WHERE parent_id IS NOT NULL")
        assert _first(await cur.fetchone()) == 0
