import asyncio
from typing import override
from uuid import uuid4

import pytest

from agent.ownership.hosted import admit_hosted_runtime, settle_hosted_runtime
from base.db import Database
from base.events.live.bus import EventBus, EventBusConfig, event_bus_config_from_settings
from base.native_process.turn_identity import HostedTurnResources
from services.agent_runner.agent_host.tests.runtime.host_support import (
    Build,
    BuildWith,
    HostPools,
    Row,
    ScriptedGraph,
)

TURN_TIMEOUT_S = 10


# meiva:note
# A bus whose first publication stays half open until released, as a Redis link that accepts
# the write and never answers; later publications go through. `publications` counts them all.
class HalfOpenBus(EventBus):
    def __init__(self, config: EventBusConfig) -> None:
        super().__init__(config)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.publications = 0

    @override
    async def publish_best_effort(
        self, payload: str, *, channel: str | None = None, context: str = ""
    ) -> int | None:
        self.publications += 1
        if self.publications == 1:
            self.entered.set()
            await self.release.wait()
        return await super().publish_best_effort(payload, channel=channel, context=context)


async def test_cancel_during_live_announce_settles_the_committed_admission(
    wired_with: BuildWith,
) -> None:
    bus = HalfOpenBus(event_bus_config_from_settings())
    host, graph, stored = wired_with({1: Row(status="idling")}, ScriptedGraph({}), bus=bus)

    task = asyncio.create_task(host._run_turn(1, resources=HostedTurnResources()))
    await asyncio.wait_for(bus.entered.wait(), timeout=TURN_TIMEOUT_S)
    assert stored.status(1) == "running"

    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=1.0)
    cancellation_settled = bool(done)
    if not cancellation_settled:
        bus.release.set()
        await asyncio.gather(task, return_exceptions=True)

    assert cancellation_settled, "announcement cancellation did not finish settlement"
    assert task.cancelled()
    assert stored.status(1) == "idling"
    assert bus.publications == 2, "settlement must announce after restoring durable status"
    assert 1 not in host._in_flight
    assert graph.observations == []


@pytest.mark.parametrize("status", ["running", "idling"])
async def test_host_takes_over_a_row_last_owned_by_another_instance(
    wired: Build, host_pools: HostPools, database: Database, event_bus: EventBus, status: str
) -> None:
    host, _, stored = wired({1: Row(status="idling")})
    original = await admit_hosted_runtime(host_pools.workload, 1, "this-box", uuid4(), db=database)
    assert original is not None
    if status == "idling":
        assert await settle_hosted_runtime(host_pools.workload, original, bus=event_bus)

    await asyncio.wait_for(host.run_turn(1), TURN_TIMEOUT_S)

    owner, generation = stored.runtime_ownership(1)
    assert owner == host.runtime_owner != original.owner
    assert generation != original.generation
