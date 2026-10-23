from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import cast

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.daemon.loop_health import LoopProgress
from base.daemon.round_loop import CONSECUTIVE_ROUND_FAILURE_LIMIT, RoundFailureLimitError
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from services.wake.heartbeat import daemon as heartbeat_daemon


def _drive(
    loop: str,
    gate: ProcessDbGate,
    outcomes: list[BaseException | None],
) -> tuple[Awaitable[None], Callable[[], int]]:
    pool = cast(ConnectionPool, object())
    database = Database.from_settings(gate=gate)
    bus = EventBus.from_settings()
    progress = LoopProgress(loop, 60.0)
    rounds = 0

    def next_outcome() -> None:
        nonlocal rounds
        if rounds == len(outcomes):
            raise asyncio.CancelledError
        outcome = outcomes[rounds]
        rounds += 1
        if outcome is not None:
            raise outcome

    async def no_sleep(_liveness: object, _total_s: float) -> None:
        return None

    async def liveness_pass(*_args: object) -> None:
        next_outcome()

    def empty_poll(*_args: object, **_kwargs: object) -> list[tuple[int, float]]:
        next_outcome()
        return []

    def nothing(*_args: object, **_kwargs: object) -> None:
        return None

    def no_deadlines(*_args: object, **_kwargs: object) -> dict[int, float]:
        return {}

    if loop == "poll":
        coro = heartbeat_daemon.dispatch_loop(
            pool,
            database,
            bus,
            progress,
            sweep_resets=lambda _pool: None,
            deps=heartbeat_daemon.DispatchLoopDeps(
                sleep_fn=no_sleep,
                reconcile_fn=nothing,
                select_fn=empty_poll,
                deadlines_fn=no_deadlines,
            ),
        )
    else:
        coro = heartbeat_daemon.liveness_loop(
            database,
            pool,
            bus,
            progress,
            pass_fn=liveness_pass,
            sleep_fn=no_sleep,
        )
    return coro, lambda: rounds


@pytest.mark.parametrize("loop", ["poll", "liveness"])
async def test_consecutive_failed_rounds_end_the_loop(
    loop: str, database_gate: ProcessDbGate
) -> None:
    limit = CONSECUTIVE_ROUND_FAILURE_LIMIT
    coro, rounds = _drive(loop, database_gate, [RuntimeError("boom")] * (limit + 3))

    with pytest.raises(RoundFailureLimitError):
        await coro

    assert rounds() == limit, "the loop must stop at the limit, not keep retrying"


@pytest.mark.parametrize("loop", ["poll", "liveness"])
async def test_a_completed_round_and_a_db_outage_do_not_exhaust_the_budget(
    loop: str, database_gate: ProcessDbGate
) -> None:
    limit = CONSECUTIVE_ROUND_FAILURE_LIMIT
    boom = RuntimeError("boom")
    outcomes: list[BaseException | None] = [boom] * (limit - 1)
    outcomes += [None]
    outcomes += [psycopg.OperationalError("db down")] * limit
    outcomes += [boom] * (limit - 1)
    coro, rounds = _drive(loop, database_gate, outcomes)

    with pytest.raises(asyncio.CancelledError):
        await coro

    assert rounds() == len(outcomes)
