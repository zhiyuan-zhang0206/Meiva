from __future__ import annotations

import asyncio
from collections.abc import Awaitable

import pytest

from base.events.live.redis_listener import RedisInboundListener

# meiva:note
# The consume grace these tests run under: the listener takes it as a constructor argument, so a
# test that names it here keeps the wait's bound and the guard derived from it the same number
# even when production's default moves.
CONSUME_GRACE_S = 2.0


# meiva:note
# A wait has two documented bounds: the caller's budget for the open phase, and that budget plus
# the consume grace for the consume phase. The guard takes twice the larger one — fixed headroom
# for scheduling jitter, which stays outside the bound at any size, so it does not scale with the
# budget. `test_redis_listener.test_the_hang_guard_leaves_room_for_both_phases` keeps it outside
# for the budgets the suite uses.
def hang_guard_s(budget_s: float) -> float:
    return 2 * (budget_s + CONSUME_GRACE_S)


# meiva:note
# Await a wait under that guard: one that never returns fails with a sentence naming the budget.
# `await_wait_one` takes the timeout and the guard's budget as one argument, so no site can hand
# the wait one number and the guard another; `await_bounded` serves the sites that start the wait
# as a task first.
async def await_bounded(awaitable: Awaitable[bool], *, budget_s: float, what: str) -> bool:
    guard = hang_guard_s(budget_s)
    try:
        return await asyncio.wait_for(awaitable, timeout=guard)
    except TimeoutError:
        pytest.fail(f"{what} did not return within {guard:.1f}s for a {budget_s}s budget")


async def await_wait_one(listener: RedisInboundListener, *, timeout_s: float, what: str) -> bool:
    return await await_bounded(listener.wait_one(timeout=timeout_s), budget_s=timeout_s, what=what)
