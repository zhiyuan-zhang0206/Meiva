from __future__ import annotations

import asyncio

import pytest
from psycopg_pool import AsyncConnectionPool

from base.clock.tests.seeded_rng import seeded_rng
from base.config.tests.configuration import snapshot_process_config
from base.db import Database
from services.agent_runner.agent_host.daemon import boot_handles
from services.agent_runner.agent_host.tests.runtime.host_support import (
    BuildWith,
    Row,
    ScriptedGraph,
)

TURN_TIMEOUT_S = 10
# meiva:note
# How long a call that must be blocked on a held pool is watched before it counts as blocked.
BLOCKED_FOR_S = 0.3


def _single_connection_pool(db_url: str) -> AsyncConnectionPool:
    return AsyncConnectionPool(
        db_url,
        min_size=1,
        max_size=1,
        timeout=TURN_TIMEOUT_S,
        kwargs={"autocommit": True, "prepare_threshold": None},
        open=False,
    )


async def _stays_blocked(task: asyncio.Task[None]) -> bool:
    done, _ = await asyncio.wait({task}, timeout=BLOCKED_FOR_S)
    return not done


class TestPoolIsolation:
    @pytest.mark.parametrize("turn_limit", [0, 2, 1000])
    def test_pool_capacity_is_independent_of_agent_admission(
        self, turn_limit: int, database: Database
    ) -> None:
        config = snapshot_process_config()
        config.set_field("host_max_concurrent_turns", turn_limit)
        config.set_field("host_db_pool_max_size", 12)
        config.set_field("host_control_pool_max_size", 3)

        workload_pool, control_pool, _, _ = boot_handles(database, config, seeded_rng())

        assert workload_pool is not control_pool
        assert workload_pool.max_size == 12
        assert control_pool.max_size == 3

    async def test_reading_the_stored_config_needs_the_control_pool_alone(
        self, wired_with: BuildWith, db_url: str
    ) -> None:
        workload = _single_connection_pool(db_url)
        control = _single_connection_pool(db_url)
        async with workload, control:
            host, _, _ = wired_with(
                {11: Row(machine="elsewhere")},
                ScriptedGraph({}),
                workload_pool=workload,
                control_pool=control,
            )

            async with workload.connection():
                await asyncio.wait_for(host.run_turn(11), TURN_TIMEOUT_S)
            assert host.stats.wakes_skipped == 1

            async with control.connection():
                skipped = asyncio.create_task(host.run_turn(11))
                assert await _stays_blocked(skipped)
            await asyncio.wait_for(skipped, TURN_TIMEOUT_S)
            assert host.stats.wakes_skipped == 2

    async def test_admission_and_settlement_wait_on_the_control_pool_not_the_turn_pool(
        self, wired_with: BuildWith, db_url: str
    ) -> None:
        graph = ScriptedGraph({})
        workload = _single_connection_pool(db_url)
        control = _single_connection_pool(db_url)
        async with workload, control:
            host, _, stored = wired_with(
                {11: Row(status="idling")},
                graph,
                workload_pool=workload,
                control_pool=control,
            )
            graph.gate(11)
            turn = asyncio.create_task(host.run_turn(11))
            await asyncio.wait_for(graph.arrival(11).wait(), TURN_TIMEOUT_S)
            assert stored.status(11) == "running", "the admission went through"
            assert graph.observations[-1].ops_pool is workload

            async with control.connection():
                graph.gate(11).set()
                assert await _stays_blocked(turn)
                assert stored.status(11) == "running", "the settlement waits for the control pool"
            await asyncio.wait_for(turn, TURN_TIMEOUT_S)

            assert stored.status(11) == "idling"
