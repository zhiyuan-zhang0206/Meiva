import asyncio

from services.agent_runner.agent_host.settlement import wait_shielded_task


async def test_repeated_cancellation_cannot_release_owned_settlement() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def settle() -> None:
        entered.set()
        await release.wait()

    owned = asyncio.create_task(settle())
    custody = asyncio.create_task(wait_shielded_task(owned))
    await entered.wait()
    custody.cancel()
    await asyncio.sleep(0)
    custody.cancel()
    await asyncio.sleep(0)
    assert not owned.cancelled() and not custody.done()
    release.set()
    assert await custody
    assert owned.done() and owned.exception() is None
