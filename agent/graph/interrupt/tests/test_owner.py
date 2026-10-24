import asyncio
from typing import cast

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool, PoolClosed

from agent.graph.interrupt import subscribe_interrupt
from agent.graph.interrupt.tests.parked_pool import ParkedPool
from agent.graph.interrupt.tests.queued_commands import queue_command
from base.db import create_agent
from base.log.tests.log_records import LogRecords
from base.native_process.turn_identity import HostedServiceResources, HostedTurnResources

_POLL_S = 0.02
_ARRIVAL_TIMEOUT_S = 5.0


def _released() -> asyncio.Event:
    released = asyncio.Event()
    released.set()
    return released


async def test_a_poll_that_fails_in_flight_reaches_the_caller_unwrapped() -> None:
    service = HostedServiceResources()
    scope = await service.turn()
    never_opened: AsyncConnectionPool = AsyncConnectionPool("dbname=never_opened", open=False)

    with pytest.raises(PoolClosed):
        async with subscribe_interrupt(never_opened, 1, resources=scope, poll_s=_POLL_S) as event:
            await asyncio.wait_for(event.wait(), _ARRIVAL_TIMEOUT_S)
    assert not service.failures
    await service.aclose()


async def test_primary_and_watcher_cleanup_unknown_both_survive() -> None:
    service = HostedServiceResources()
    scope = await service.turn()
    primary, secondary = RuntimeError("body failed"), ValueError("watcher cleanup failed")
    pool = ParkedPool(release=_released(), after_cancel=secondary)

    with pytest.raises(RuntimeError) as observed:
        async with subscribe_interrupt(pool, 1, resources=scope, poll_s=_POLL_S):
            await pool.parked.wait()
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
    release, sibling_alive = asyncio.Event(), asyncio.Event()
    failure = RuntimeError("late poll failed")
    pool = ParkedPool(release=release, after_cancel=failure)

    async def other_turn() -> None:
        await release.wait()
        await asyncio.sleep(0)
        sibling_alive.set()

    async with subscribe_interrupt(
        pool, 1, resources=scope, poll_s=_POLL_S, exit_timeout_s=0.02
    ) as event:
        await pool.parked.wait()
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
    release = asyncio.Event()
    failure = ValueError("after repeated caller cancel")
    pool = ParkedPool(release=release, after_cancel=failure)

    async def invoke() -> None:
        async with subscribe_interrupt(
            pool, 1, resources=scope, poll_s=_POLL_S, exit_timeout_s=0.03
        ):
            await pool.parked.wait()

    caller = asyncio.create_task(invoke())
    await pool.cancelled.wait()
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
    pool = ParkedPool(release=release)

    started = asyncio.get_running_loop().time()
    async with subscribe_interrupt(
        pool, 1, resources=hosted_resources, poll_s=_POLL_S, exit_timeout_s=0.2
    ):
        await pool.parked.wait()
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 2.0, f"exit took {elapsed:.2f}s — the bounded handoff did not bound"
    (abandoned,) = [r for r in loguru_records if "retained by its service" in r["message"]]
    assert abandoned["level"].name == "INFO"
    assert "wedged await chain" in abandoned["message"]
    assert "in pending_interrupt_reason" in abandoned["message"]

    release.set()
    await asyncio.sleep(0.01)
    (fate,) = [r["message"] for r in loguru_records if "retained interrupt watcher" in r["message"]]
    assert "cancelled" in fate


async def test_exit_still_reaps_a_prompt_watcher_without_a_service_handoff(
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    loguru_records: LogRecords,
) -> None:
    async with subscribe_interrupt(aops_pool, 1, resources=hosted_resources, poll_s=_POLL_S):
        await asyncio.sleep(5 * _POLL_S)
    assert not any("retained by its service" in r["message"] for r in loguru_records)


async def test_a_watcher_that_swallowed_its_cancel_ends_at_its_poll_without_firing(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    loguru_records: LogRecords,
) -> None:
    agent_id = create_agent(db_conn)
    queue_command(db_conn, agent_id, "cancel")
    pool = ParkedPool(release=_released(), real=aops_pool)

    async with subscribe_interrupt(
        pool, agent_id, resources=hosted_resources, poll_s=_POLL_S, exit_timeout_s=1.0
    ) as event:
        await pool.parked.wait()

    assert pool.cancelled.is_set()
    assert not event.is_set()
    assert not any("retained by its service" in r["message"] for r in loguru_records)
