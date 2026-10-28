import asyncio
import contextlib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from uuid import uuid4

import pytest

from agent.ownership.hosted import admit_hosted_runtime, settle_hosted_runtime
from base.clock import SYSTEM_TIMERS
from base.clock.tests.seeded_rng import seeded_rng
from base.db import Database
from base.events.live.bus import EventBus, EventBusConfig
from base.native_process.turn_identity import HostedTurnResources
from services.agent_runner.agent_host.tests.runtime.host_support import (
    Build,
    BuildWith,
    HostPools,
    Row,
    ScriptedGraph,
)

TURN_TIMEOUT_S = 10


@dataclass
class _HalfOpenRedisProbe:
    bus: EventBus
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    publications: int = 0


# meiva:note
# A Redis listener whose first publication stays half open until released, as a Redis link
# that accepts the write and never answers; later publications receive a normal RESP reply.
@asynccontextmanager
async def half_open_redis_bus() -> AsyncGenerator[_HalfOpenRedisProbe]:
    probe: _HalfOpenRedisProbe | None = None
    writers: list[asyncio.StreamWriter] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        assert probe is not None
        writers.append(writer)
        try:
            with contextlib.suppress(ConnectionError):
                while await reader.read(4096):
                    probe.publications += 1
                    if probe.publications == 1:
                        probe.entered.set()
                        await probe.release.wait()
                    writer.write(b":1\r\n")
                    await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handle, host="127.0.0.1", port=0)
    try:
        port = server.sockets[0].getsockname()[1]
        bus = EventBus(
            EventBusConfig(
                redis_url=f"redis://127.0.0.1:{port}",
                events_channel="test",
                publish_attempt_timeout_s=60.0,
            ),
            rng=seeded_rng(),
            timers=SYSTEM_TIMERS,
        )
        probe = _HalfOpenRedisProbe(bus=bus)
        yield probe
    finally:
        if probe is not None:
            probe.release.set()
        for writer in writers:
            writer.close()
        server.close()
        await server.wait_closed()


async def test_cancel_during_live_announce_settles_the_committed_admission(
    wired_with: BuildWith,
) -> None:
    async with half_open_redis_bus() as probe:
        host, graph, stored = wired_with(
            {1: Row(status="idling")}, ScriptedGraph({}), bus=probe.bus
        )

        task = asyncio.create_task(host._run_turn(1, resources=HostedTurnResources()))
        await asyncio.wait_for(probe.entered.wait(), timeout=TURN_TIMEOUT_S)
        assert stored.status(1) == "running"

        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=1.0)
        cancellation_settled = bool(done)
        if not cancellation_settled:
            probe.release.set()
            await asyncio.gather(task, return_exceptions=True)

        assert cancellation_settled, "announcement cancellation did not finish settlement"
        assert task.cancelled()
        assert stored.status(1) == "idling"
        assert probe.publications == 2, "settlement must announce after restoring durable status"
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
