import asyncio
from collections.abc import Coroutine
from contextlib import AbstractAsyncContextManager
from typing import Any, cast

import pytest

from agent.graph.interrupt import InterruptEvent
from agent.graph.interrupt._watch import owned_watch
from base.log.tests.log_records import LogRecords
from base.native_process.turn_identity import HostedServiceResources, HostedTurnResources


def _own(
    scope: HostedTurnResources,
    body: Coroutine[Any, Any, None],
    *,
    exit_timeout_s: float = 5.0,
    event: InterruptEvent | None = None,
    stop: asyncio.Event | None = None,
) -> AbstractAsyncContextManager[InterruptEvent]:
    return owned_watch(
        scope,
        1,
        event if event is not None else InterruptEvent(),
        stop if stop is not None else asyncio.Event(),
        body,
        exit_timeout_s=exit_timeout_s,
    )


async def test_inflight_unknown_reaches_caller_with_original_identity() -> None:
    service = HostedServiceResources()
    scope = await service.turn()
    original = ValueError("invalid watcher boundary")

    async def fail() -> None:
        raise original

    with pytest.raises(ValueError) as observed:
        async with _own(scope, fail()) as event:
            await event.wait()
    assert observed.value is original
    assert not service.failures
    await service.aclose()


async def test_primary_and_watcher_cleanup_unknown_both_survive() -> None:
    service = HostedServiceResources()
    scope = await service.turn()
    entered = asyncio.Event()
    primary, secondary = RuntimeError("body failed"), ValueError("watcher cleanup failed")

    async def fail_cleanup() -> None:
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            raise secondary from None

    with pytest.raises(RuntimeError) as observed:
        async with _own(scope, fail_cleanup()):
            await entered.wait()
            raise primary
    assert observed.value is primary
    cause = cast(ExceptionGroup[Exception], primary.__cause__)
    assert isinstance(cause, ExceptionGroup)
    assert cause.exceptions == (secondary,)
    assert not service.failures
    await service.aclose()


async def test_late_unknown_is_visible_before_service_join_without_sibling_cancel(
    loguru_records: LogRecords,
) -> None:
    service = HostedServiceResources()
    scope = await service.turn()
    entered, release, sibling_alive = asyncio.Event(), asyncio.Event(), asyncio.Event()
    failure = RuntimeError("late poll failed")
    event = InterruptEvent()

    async def late_failure() -> None:
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            await release.wait()
            raise failure from None

    async def other_turn() -> None:
        await release.wait()
        await asyncio.sleep(0)
        sibling_alive.set()

    async with _own(scope, late_failure(), exit_timeout_s=0.02, event=event):
        await entered.wait()
    assert not event.is_set()
    assert not service.failures
    service.watch(other_turn(), name="other-agent")
    release.set()
    await sibling_alive.wait()
    assert service.failures == [(scope, failure)]
    assert not event.is_set()
    assert any("retained until service stop/join" in record["message"] for record in loguru_records)
    with pytest.raises(RuntimeError) as observed:
        await service.aclose()
    assert observed.value is failure


async def test_repeated_cancel_during_bounded_exit_still_hands_off_actual_task() -> None:
    service = HostedServiceResources()
    scope = await service.turn()
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    failure = ValueError("after repeated caller cancel")

    async def late_failure() -> None:
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            raise failure from None

    async def invoke() -> None:
        async with _own(scope, late_failure(), exit_timeout_s=0.03):
            await entered.wait()

    caller = asyncio.create_task(invoke())
    await cancelled.wait()
    caller.cancel()
    await asyncio.sleep(0)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(caller, 0.5)
    release.set()
    with pytest.raises(ValueError) as observed:
        await service.aclose()
    assert observed.value is failure


async def test_exit_abandons_a_wedged_watcher_within_the_bound_and_names_where_it_is_stuck(
    hosted_resources: HostedTurnResources, loguru_records: LogRecords
) -> None:
    release = asyncio.Event()

    async def wedged() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await release.wait()
            raise

    started = asyncio.get_running_loop().time()
    async with _own(hosted_resources, wedged(), exit_timeout_s=0.2):
        await asyncio.sleep(0)
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 2.0, f"exit took {elapsed:.2f}s — the bounded handoff did not bound"
    (abandoned,) = [r for r in loguru_records if "retained by its service" in r["message"]]
    assert abandoned["level"].name == "INFO"
    assert "wedged await chain" in abandoned["message"]
    assert "in wedged" in abandoned["message"]

    release.set()
    await asyncio.sleep(0.01)
    (fate,) = [r["message"] for r in loguru_records if "retained interrupt watcher" in r["message"]]
    assert "cancelled" in fate


async def test_exit_still_reaps_a_prompt_watcher_without_a_service_handoff(
    hosted_resources: HostedTurnResources, loguru_records: LogRecords
) -> None:
    async def healthy() -> None:
        await asyncio.sleep(3600)

    async with _own(hosted_resources, healthy()):
        await asyncio.sleep(0)
    assert not any("retained by its service" in r["message"] for r in loguru_records)
