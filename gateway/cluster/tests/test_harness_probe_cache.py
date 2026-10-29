import asyncio

import pytest

from base.api_contracts.agent_contract import Harness, HarnessBilling
from gateway.cluster.harness_probe import HarnessProbeCache
from gateway.cluster.schemas import HarnessAvailability


def rows(reason: str) -> list[HarnessAvailability]:
    return [
        HarnessAvailability(
            harness=Harness.MEIVA,
            status="available",
            reason=reason,
            launchable=True,
            billing=HarnessBilling.API,
        )
    ]


@pytest.mark.asyncio
async def test_refresh_shares_work_and_survives_a_disconnected_follower() -> None:
    cache = HarnessProbeCache()
    started = asyncio.Event()
    joined = asyncio.Event()
    release = asyncio.Event()
    calls = 0
    expected = rows("checked")

    async def probe() -> list[HarnessAvailability]:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return expected

    async def follow() -> list[HarnessAvailability]:
        joined.set()
        return await cache.refresh("runner", probe)

    assert cache.get("runner") is None
    leader = asyncio.create_task(cache.refresh("runner", probe))
    await started.wait()
    cancelled_follower = asyncio.create_task(follow())
    await joined.wait()
    cancelled_follower.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_follower
    follower = asyncio.create_task(cache.refresh("runner", probe))
    release.set()
    assert await follower == await leader == expected
    assert calls == 1
    assert cache.get("runner") == expected


@pytest.mark.asyncio
async def test_failed_refresh_preserves_the_snapshot_and_can_be_retried() -> None:
    cache = HarnessProbeCache()
    old = rows("previous")
    cache.store("runner", old)

    async def fail() -> list[HarnessAvailability]:
        raise RuntimeError("runner unreachable")

    with pytest.raises(RuntimeError, match="runner unreachable"):
        await cache.refresh("runner", fail)
    assert cache.get("runner") == old

    async def succeed() -> list[HarnessAvailability]:
        return rows("updated")

    assert await cache.refresh("runner", succeed) == rows("updated")
    assert cache.get("runner") == rows("updated")


@pytest.mark.asyncio
async def test_cancelled_leader_settles_followers_and_preserves_the_snapshot() -> None:
    cache = HarnessProbeCache()
    started = asyncio.Event()
    joined = asyncio.Event()
    exited = asyncio.Event()
    cache.store("runner", rows("previous"))

    async def probe() -> list[HarnessAvailability]:
        try:
            started.set()
            await asyncio.Event().wait()
            return rows("unreachable")
        finally:
            exited.set()

    async def follow() -> list[HarnessAvailability]:
        joined.set()
        return await cache.refresh("runner", probe)

    leader = asyncio.create_task(cache.refresh("runner", probe))
    await started.wait()
    follower = asyncio.create_task(follow())
    await joined.wait()
    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    with pytest.raises(asyncio.CancelledError):
        await follower
    assert exited.is_set()
    assert cache.get("runner") == rows("previous")

    async def retry() -> list[HarnessAvailability]:
        return rows("retried")

    assert await cache.refresh("runner", retry) == rows("retried")
