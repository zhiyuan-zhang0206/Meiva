import asyncio

import pytest

from services.agent_runner.agent_host.tests.runtime.host_support import Build, Row

TURN_TIMEOUT_S = 10


async def test_business_error_remains_primary_and_is_joined_at_stop(wired: Build) -> None:
    primary = ValueError("original hosted business failure")
    host, _, _ = wired({7: Row()}, {7: [primary]})

    with pytest.raises(ValueError) as observed:
        await asyncio.wait_for(host.run_turn(7), TURN_TIMEOUT_S)
    assert observed.value is primary
    with pytest.raises(ValueError) as joined:
        await host._resource_service.aclose()
    assert joined.value is primary


async def test_root_cancellation_cancels_the_work_and_joins_it(wired: Build) -> None:
    host, graph, _ = wired({7: Row()})
    graph.gate(7)
    root = asyncio.create_task(host.run_turn(7))
    try:
        await asyncio.wait_for(graph.arrival(7).wait(), TURN_TIMEOUT_S)
        [actual_work] = [
            task
            for task in host._resource_service._pending
            if getattr(task.get_coro(), "__qualname__", "").endswith("AgentHost._run_turn")
        ]
        root.cancel()
        with pytest.raises(asyncio.CancelledError):
            await root
        assert actual_work.cancelled()
        await host._resource_service.aclose()
        assert host.resources_joined
    finally:
        root.cancel()
