import asyncio
from dataclasses import replace

from services.agent_runner.agent_host.runtime import HostCachePolicy
from services.agent_runner.agent_host.tests.host_policy import configured_policy
from services.agent_runner.agent_host.tests.runtime.host_support import Build, Row

# meiva:note
# Three chat models of the real catalog, standing for three different cluster defaults.
ALPHA = "claude-haiku-5-5"
BETA = "claude-sonnet-5-5"
TURN_TIMEOUT_S = 10


async def test_host_capacity_is_independent_and_fixed_at_construction(wired: Build) -> None:
    policy = configured_policy(max_concurrent_turns=1, default_model=ALPHA)
    first, _, _ = wired({}, policy=policy)
    second, _, _ = wired({}, policy=replace(policy, max_concurrent_turns=2))
    entered = asyncio.Event()

    async def queued() -> None:
        async with first.admission.admit(2):
            entered.set()

    async with first.admission.admit(1):
        task = asyncio.create_task(queued())
        await asyncio.sleep(0)
        assert first.admission.waiting == 1
        async with second.admission.admit(1), second.admission.admit(2):
            assert second.admission.waiting == 0
            assert not entered.is_set()
    await task
    assert entered.is_set()
    assert first.admission.limit == 1
    assert second.admission.limit == 2


async def test_each_host_runs_its_own_model_default_and_an_explicit_pin_outranks_it(
    wired: Build,
) -> None:
    first_policy = configured_policy(max_concurrent_turns=0, default_model=ALPHA)
    second_policy = replace(first_policy, default_model=BETA)
    first, first_graph, _ = wired({1: Row()}, policy=first_policy)
    second, second_graph, _ = wired({2: Row()}, policy=second_policy)
    await asyncio.wait_for(asyncio.gather(first.run_turn(1), second.run_turn(2)), TURN_TIMEOUT_S)
    assert first_graph.observations[-1].model == ALPHA
    assert first_graph.observations[-1].served_model == ALPHA
    assert second_graph.observations[-1].model == BETA

    pinned, graph, _ = wired({3: Row(overlay={"llm_model": ALPHA})}, policy=second_policy)
    await asyncio.wait_for(pinned.run_turn(3), TURN_TIMEOUT_S)
    assert graph.observations[-1].model == ALPHA, "an explicit pin outranks the host's default"
    await asyncio.gather(first.aclose(), second.aclose(), pinned.aclose())


async def test_each_host_keeps_runtimes_by_its_own_cache_bounds(wired: Build) -> None:
    first_policy = configured_policy(max_concurrent_turns=0, cache=HostCachePolicy(60.0, 4))
    second_policy = replace(first_policy, cache=HostCachePolicy(60.0, 1))
    first, _, _ = wired({1: Row(), 2: Row(), 3: Row()}, policy=first_policy)
    second, _, _ = wired({4: Row(), 5: Row(), 6: Row()}, policy=second_policy)
    for agent_id in (1, 2, 3):
        await asyncio.wait_for(first.run_turn(agent_id), TURN_TIMEOUT_S)
    for agent_id in (4, 5, 6):
        await asyncio.wait_for(second.run_turn(agent_id), TURN_TIMEOUT_S)

    assert list(first._runtimes) == [1, 2, 3]
    assert list(second._runtimes) == [6], "the least recently used are evicted down to the bound"
    await asyncio.gather(first.aclose(), second.aclose())
