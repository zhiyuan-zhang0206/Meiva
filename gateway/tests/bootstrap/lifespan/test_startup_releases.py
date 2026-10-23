# meiva:note
# The start-up release policy of the gateway lifespan, driven with plain steps.
#
# `startup_releases` is the component the lifespan registers every acquired resource with. These
# tests drive it with local functions as the releases, because the real resources cannot be made to
# fail on demand: they pin what each failure shape (a failing release, a start-up error, a
# cancellation) does to the releases registered before it.

import asyncio

import pytest

from gateway.app import startup_releases


async def test_releases_run_last_acquired_first() -> None:
    order: list[str] = []

    async def closes_client() -> None:
        order.append("client")

    async with startup_releases() as releases:
        releases.callback(lambda: order.append("pool"))
        releases.push_async_callback(closes_client)
        releases.callback(lambda: order.append("monitor"))

    assert order == ["monitor", "client", "pool"]


async def test_a_failing_release_never_skips_the_rest_and_its_error_is_raised() -> None:
    order: list[str] = []
    failure = RuntimeError("data-plane pool close failed")

    def fails() -> None:
        order.append("data")
        raise failure

    with pytest.raises(RuntimeError) as raised:
        async with startup_releases() as releases:
            releases.callback(fails)
            releases.callback(lambda: order.append("control"))
            releases.callback(lambda: order.append("client"))

    assert raised.value is failure
    assert order == ["client", "control", "data"]


async def test_a_start_up_error_closes_what_was_acquired_once_and_is_the_error_raised() -> None:
    order: list[str] = []
    failure = RuntimeError("control pool construction failed")

    with pytest.raises(RuntimeError) as raised:
        async with startup_releases() as releases:
            releases.callback(lambda: order.append("data"))
            raise failure

    assert raised.value is failure
    assert order == ["data"]


async def test_a_release_failing_during_a_start_up_error_is_a_note_on_that_error() -> None:
    order: list[str] = []
    primary = RuntimeError("upload recovery construction failed")
    cleanup = RuntimeError("monitor stop failed")

    def fails() -> None:
        order.append("monitor")
        raise cleanup

    with pytest.raises(RuntimeError) as raised:
        async with startup_releases() as releases:
            releases.callback(lambda: order.append("pool"))
            releases.callback(fails)
            raise primary

    assert raised.value is primary
    assert order == ["monitor", "pool"]
    assert any("monitor stop failed" in note for note in primary.__notes__)


async def test_a_cancelled_body_still_runs_every_release() -> None:
    order: list[str] = []
    entered = asyncio.Event()

    async def serve() -> None:
        async with startup_releases() as releases:
            releases.callback(lambda: order.append("pool"))
            releases.callback(lambda: order.append("client"))
            entered.set()
            await asyncio.Event().wait()

    running = asyncio.create_task(serve())
    await entered.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert order == ["client", "pool"]
