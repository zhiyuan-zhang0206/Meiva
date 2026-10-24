import asyncio

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.graph.interrupt import InterruptEvent, subscribe_interrupt
from agent.graph.interrupt._watch import owned_watch, watch_for_interrupt
from agent.graph.interrupt.tests.queued_commands import queue_command
from base.agents.messages.inbound import InterruptReason
from base.db import create_agent
from base.db.tests.parked_pool import ParkedAsyncPool
from base.native_process.turn_identity import HostedTurnResources

_ARRIVAL_TIMEOUT_S = 5.0
_POLL_S = 0.02


async def test_a_pending_cancel_fires_the_event_with_the_reason_of_its_source_and_stays_pending(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent_id = create_agent(db_conn)
    queue_command(db_conn, agent_id, "cancel", source="agent:9")
    event = InterruptEvent()

    await asyncio.wait_for(
        watch_for_interrupt(aops_pool, event, agent_id, asyncio.Event(), poll_s=_POLL_S),
        _ARRIVAL_TIMEOUT_S,
    )

    assert event.is_set()
    assert event.reason is InterruptReason.SYSTEM
    row = db_conn.execute(
        "SELECT status FROM inbound_messages WHERE agent_id=%s", (agent_id,)
    ).fetchone()
    assert row == ("pending",)


async def test_the_watcher_keeps_polling_until_a_cancel_is_queued(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent_id = create_agent(db_conn)
    queue_command(db_conn, agent_id, "chat")
    event = InterruptEvent()
    watcher = asyncio.create_task(
        watch_for_interrupt(aops_pool, event, agent_id, asyncio.Event(), poll_s=_POLL_S)
    )

    await asyncio.sleep(10 * _POLL_S)
    assert not event.is_set()
    queue_command(db_conn, agent_id, "terminate")
    await asyncio.wait_for(watcher, _ARRIVAL_TIMEOUT_S)

    assert event.is_set()
    assert event.reason is InterruptReason.USER


@pytest.mark.parametrize("cancel_is_pending", [False, True])
async def test_a_poll_that_returns_after_stop_ends_the_watcher_without_firing(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, cancel_is_pending: bool
) -> None:
    agent_id = create_agent(db_conn)
    if cancel_is_pending:
        queue_command(db_conn, agent_id, "cancel")
    event, stop = InterruptEvent(), asyncio.Event()
    pool = ParkedAsyncPool(aops_pool, park_on=1)
    watcher = asyncio.create_task(watch_for_interrupt(pool, event, agent_id, stop, poll_s=_POLL_S))
    await asyncio.wait_for(pool.parked.wait(), _ARRIVAL_TIMEOUT_S)
    assert not watcher.done()
    stop.set()
    pool.release()
    await asyncio.wait_for(watcher, _ARRIVAL_TIMEOUT_S)

    assert not event.is_set()


async def test_a_borrow_that_swallows_the_watchers_cancel_still_ends_after_stop_without_firing(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent_id = create_agent(db_conn)
    queue_command(db_conn, agent_id, "cancel")
    event, stop = InterruptEvent(), asyncio.Event()
    pool = ParkedAsyncPool(aops_pool, park_on=1, swallow_cancel=True)
    watcher = asyncio.create_task(watch_for_interrupt(pool, event, agent_id, stop, poll_s=_POLL_S))
    await asyncio.wait_for(pool.parked.wait(), _ARRIVAL_TIMEOUT_S)
    stop.set()
    watcher.cancel()
    while not pool.cancellations_swallowed:
        await asyncio.sleep(_POLL_S)
    assert not watcher.done()
    pool.release()
    await asyncio.wait_for(watcher, _ARRIVAL_TIMEOUT_S)

    assert not event.is_set()


async def test_a_watcher_that_swallowed_its_cancel_ends_at_its_next_check_without_firing(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = create_agent(db_conn)
    queue_command(db_conn, agent_id, "cancel")
    event, stop = InterruptEvent(), asyncio.Event()
    entered, swallowed, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def swallows_one_cancel() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            swallowed.set()
        await watch_for_interrupt(aops_pool, event, agent_id, stop, poll_s=_POLL_S)
        finished.set()

    async with owned_watch(
        hosted_resources, agent_id, event, stop, swallows_one_cancel(), exit_timeout_s=1.0
    ):
        await entered.wait()

    assert swallowed.is_set()
    assert finished.is_set()
    assert not event.is_set()


@pytest.mark.parametrize("missing_scope", [True, False])
async def test_a_database_subscription_refuses_an_unowned_watcher(
    aops_pool: AsyncConnectionPool, missing_scope: bool
) -> None:
    scope = None if missing_scope else HostedTurnResources()
    with pytest.raises(RuntimeError, match="service"):
        async with subscribe_interrupt(aops_pool, 1, resources=scope):
            pytest.fail("an unowned watcher must not start")


async def test_a_subscription_without_a_pool_never_fires() -> None:
    async with subscribe_interrupt(None, 1, resources=None) as event:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(event.wait(), timeout=0.2)
        assert not event.is_set()


@pytest.mark.parametrize(
    ("kind", "fires"), [("cancel", True), ("terminate", True), ("chat", False)]
)
async def test_a_subscription_fires_for_a_command_that_was_already_pending_but_not_for_a_chat(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    kind: str,
    fires: bool,
) -> None:
    agent_id = create_agent(db_conn)
    queue_command(db_conn, agent_id, kind)
    async with subscribe_interrupt(
        aops_pool, agent_id, resources=hosted_resources, poll_s=_POLL_S
    ) as event:
        if fires:
            await asyncio.wait_for(event.wait(), timeout=_ARRIVAL_TIMEOUT_S)
        else:
            await asyncio.sleep(10 * _POLL_S)
        assert event.is_set() is fires


async def test_a_subscription_fires_on_a_cancel_queued_after_it_began(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = create_agent(db_conn)
    async with subscribe_interrupt(
        aops_pool, agent_id, resources=hosted_resources, poll_s=_POLL_S
    ) as event:
        assert not event.is_set()
        queue_command(db_conn, agent_id, "cancel")
        await asyncio.wait_for(event.wait(), timeout=_ARRIVAL_TIMEOUT_S)
        assert event.reason is InterruptReason.USER
