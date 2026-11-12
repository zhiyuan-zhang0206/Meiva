# meiva:note
# The verdict on the upstream's health and the worst-case time to recovery of each rule.
#
# Time is injected: the watchdog and the guard sleep on `ManualTimers`, which advance without
# waiting, and the probe timeout is a few milliseconds, so "no reply" is a session whose
# ping never returns. The bound each rule documents is asserted from what the rule did.
from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, cast

import anyio
import pytest
from mcp import ClientSession, types
from mcp.shared.exceptions import MCPError
from mcp.types import CONNECTION_CLOSED, REQUEST_TIMEOUT

from base.clock.tests.manual_timers import ManualTimers
from services.desktop.browser.mcp_upstream import (
    CALL_TIMEOUT_LIMIT,
    CALL_TIMEOUT_WINDOW_S,
    READ_TIMEOUT,
    UPSTREAM_DOWN_MSG,
    UPSTREAM_PROBE,
    DeathReason,
    ProbePolicy,
    UpstreamCallTimeoutError,
    UpstreamGuard,
    upstream_watchdog,
)

FAST = ProbePolicy(interval_s=30.0, timeout_s=0.01, retry_s=10.0, max_unanswered=3)


# meiva:note
# A fake upstream: `pings` is the script of ping outcomes (the last one repeats).
class Session:
    def __init__(self, pings: Sequence[str] = ("ok",), *, calls: Sequence[Exception | None] = ()):
        self._pings = list(pings)
        self._calls = list(calls)
        self.ping_count = 0
        self.calls_made = 0

    async def send_ping(self) -> None:
        outcome = self._pings[min(self.ping_count, len(self._pings) - 1)]
        self.ping_count += 1
        if outcome == "hang":
            await asyncio.Event().wait()
        elif outcome == "closed":
            raise anyio.ClosedResourceError
        elif outcome == "closed-error":
            raise MCPError(code=CONNECTION_CLOSED, message="Connection closed")
        elif outcome == "error-reply":
            raise MCPError(code=-32603, message="internal error")

    async def call_tool(self, name: str, args: dict[str, Any]) -> types.CallToolResult:
        self.calls_made += 1
        outcome = self._calls.pop(0) if self._calls else None
        if outcome is not None:
            raise outcome
        return types.CallToolResult(content=[], is_error=False)


def guard_of(
    session: Session, timers: ManualTimers | None = None, policy: ProbePolicy = FAST
) -> UpstreamGuard:
    return UpstreamGuard(cast(ClientSession, session), timers or ManualTimers(), policy)


async def ask(guard: UpstreamGuard) -> types.CallToolResult:
    return await guard.request("browser tool 'x'", lambda: guard._session.call_tool("x", {}))


def timeout_error() -> MCPError:
    return MCPError(code=REQUEST_TIMEOUT, message="Request 'tools/call' timed out")


# meiva:note
# ── The documented bounds ────────────────────────────────────────────────────


def test_the_production_policy_recovers_a_wedged_upstream_within_80_seconds() -> None:
    # meiva:note
    # 30 s to the next ping + 10 s timeout, then two retries 10 s apart, each 10 s to time out.
    assert ProbePolicy(30.0, 10.0, 10.0, 3) == UPSTREAM_PROBE
    assert UPSTREAM_PROBE.worst_case_s == 80.0


def test_repeated_call_timeouts_recover_within_three_read_timeouts_and_three_probes() -> None:
    assert (CALL_TIMEOUT_LIMIT, CALL_TIMEOUT_WINDOW_S) == (3, 1800.0)
    worst = CALL_TIMEOUT_LIMIT * (READ_TIMEOUT.total_seconds() + UPSTREAM_PROBE.timeout_s)
    assert worst == 570.0
    # meiva:note
    # a pattern that survives (limit - 1 timeouts per window) blocks everyone this long per window
    assert (CALL_TIMEOUT_LIMIT - 1) * (READ_TIMEOUT.total_seconds() + 10.0) == 380.0


def test_the_probe_after_a_timeout_is_bounded_by_the_watchdogs_policy() -> None:
    retries = UPSTREAM_PROBE.max_unanswered - 1
    assert (
        UPSTREAM_PROBE.timeout_s + retries * (UPSTREAM_PROBE.retry_s + UPSTREAM_PROBE.timeout_s)
        == 50.0
    )


# meiva:note
# ── The watchdog ─────────────────────────────────────────────────────────────


async def test_a_wedge_is_declared_after_max_unanswered_probes_within_the_bound() -> None:
    timers = ManualTimers()
    guard = guard_of(Session(["ok", "hang"]), timers)

    await upstream_watchdog(guard, asyncio.Event())

    assert guard.dead.is_set() and guard.reason is DeathReason.PROBES_UNANSWERED
    # meiva:note
    # one answered ping, then the wait for the next, then three silent probes with two retries;
    # the same schedule with the production timeout is the documented bound
    assert timers.sleeps == [30.0, 10.0, 10.0]
    assert sum(timers.sleeps) + FAST.max_unanswered * UPSTREAM_PROBE.timeout_s == (
        UPSTREAM_PROBE.worst_case_s
    )


async def test_one_unanswered_ping_between_answered_ones_is_forgiven() -> None:
    session = Session(["ok", "hang", "ok", "hang", "hang", "ok"])
    guard = guard_of(session)
    stop = asyncio.Event()
    task = asyncio.create_task(upstream_watchdog(guard, stop))
    while session.ping_count < 6:
        await asyncio.sleep(0)
    stop.set()
    await task

    assert not guard.dead.is_set()


async def test_the_streak_restarts_after_an_answer() -> None:
    # meiva:note
    # hang, hang, ok, hang, hang: never three in a row
    session = Session(["hang", "hang", "ok", "hang", "hang", "ok"])
    guard = guard_of(session)
    stop = asyncio.Event()
    task = asyncio.create_task(upstream_watchdog(guard, stop))
    while session.ping_count < 6:
        await asyncio.sleep(0)
    stop.set()
    await task

    assert not guard.dead.is_set()


@pytest.mark.parametrize("closed", ["closed", "closed-error"])
async def test_a_closed_connection_is_dead_at_once_without_repetition(closed: str) -> None:
    timers = ManualTimers()
    guard = guard_of(Session(["ok", closed]), timers)

    await upstream_watchdog(guard, asyncio.Event())

    assert guard.reason is DeathReason.UPSTREAM_EXITED
    # meiva:note
    # the second ping, answered by a closed stream, ends it
    assert timers.sleeps == [30.0]


async def test_an_error_reply_counts_like_silence_and_is_not_an_exit() -> None:
    guard = guard_of(Session(["error-reply"]))

    await upstream_watchdog(guard, asyncio.Event())

    assert guard.reason is DeathReason.PROBES_UNANSWERED


async def test_stopping_during_the_wait_ends_the_watchdog_without_a_verdict() -> None:
    stop = asyncio.Event()
    guard = guard_of(Session(["ok"]), None)
    task = asyncio.create_task(upstream_watchdog(guard, stop))
    await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(task, 1.0)

    assert not guard.dead.is_set() and guard.reason is None


# meiva:note
# ── A call that outlives the read timeout ───────────────────────────────────


async def test_a_slow_call_fails_alone_while_the_upstream_still_answers() -> None:
    guard = guard_of(Session(["ok"], calls=[timeout_error()]))

    with pytest.raises(UpstreamCallTimeoutError, match="did not finish within 180s"):
        await ask(guard)

    assert not guard.dead.is_set()


async def test_a_call_that_times_out_while_pings_are_silent_ends_the_generation() -> None:
    guard = guard_of(Session(["hang"], calls=[timeout_error()]))

    with pytest.raises(RuntimeError, match=UPSTREAM_DOWN_MSG):
        await ask(guard)

    assert guard.reason is DeathReason.CALL_TIMEOUT_UNANSWERED


async def test_a_call_that_times_out_on_a_closed_connection_is_an_exit() -> None:
    guard = guard_of(Session(["closed"], calls=[timeout_error()]))

    with pytest.raises(RuntimeError, match=UPSTREAM_DOWN_MSG):
        await ask(guard)

    assert guard.reason is DeathReason.UPSTREAM_EXITED


async def test_a_stalled_loop_that_answers_the_second_probe_after_a_timeout_is_forgiven() -> None:
    timers = ManualTimers()
    guard = guard_of(Session(["hang", "ok"], calls=[timeout_error()]), timers)

    with pytest.raises(UpstreamCallTimeoutError):
        await ask(guard)

    assert not guard.dead.is_set() and timers.sleeps == [10.0]


async def test_three_silent_probes_after_a_timeout_end_the_generation_within_the_bound() -> None:
    timers = ManualTimers()
    guard = guard_of(Session(["hang"], calls=[timeout_error()]), timers)

    with pytest.raises(RuntimeError, match=UPSTREAM_DOWN_MSG):
        await ask(guard)

    assert guard.reason is DeathReason.CALL_TIMEOUT_UNANSWERED
    assert timers.sleeps == [10.0, 10.0]


async def test_timeouts_within_the_window_end_the_generation_though_calls_succeed_between() -> None:
    errors: list[Exception | None] = [timeout_error(), None, timeout_error(), None, timeout_error()]
    guard = guard_of(Session(["ok"], calls=errors))

    for _ in range(2):
        with pytest.raises(UpstreamCallTimeoutError):
            await ask(guard)
        await ask(guard)
    assert not guard.dead.is_set()
    with pytest.raises(RuntimeError, match=UPSTREAM_DOWN_MSG):
        await ask(guard)

    assert guard.reason is DeathReason.CALL_TIMEOUTS_REPEATED


async def test_timeouts_older_than_the_window_do_not_count() -> None:
    timers = ManualTimers()
    guard = guard_of(Session(["ok"], calls=[timeout_error()] * 5), timers)

    for _ in range(5):
        with pytest.raises(UpstreamCallTimeoutError):
            await ask(guard)
        timers.advance(CALL_TIMEOUT_WINDOW_S / (CALL_TIMEOUT_LIMIT - 1) + 1)

    assert not guard.dead.is_set()


async def test_a_dead_generation_refuses_further_requests_without_asking_the_upstream() -> None:
    session = Session(["ok"])
    guard = guard_of(session)
    guard.mark_dead(DeathReason.UPSTREAM_EXITED)

    with pytest.raises(RuntimeError, match=UPSTREAM_DOWN_MSG):
        await ask(guard)

    assert session.calls_made == 0


@pytest.mark.parametrize(
    "error",
    [anyio.ClosedResourceError(), anyio.BrokenResourceError(), anyio.EndOfStream()],
)
async def test_a_closed_stream_during_a_call_is_an_exit(error: Exception) -> None:
    guard = guard_of(Session(calls=[error]))

    with pytest.raises(RuntimeError, match=UPSTREAM_DOWN_MSG):
        await ask(guard)

    assert guard.reason is DeathReason.UPSTREAM_EXITED


async def test_connection_closed_during_a_call_is_an_exit() -> None:
    error = MCPError(code=CONNECTION_CLOSED, message="Connection closed")
    guard = guard_of(Session(calls=[error]))

    with pytest.raises(RuntimeError, match=UPSTREAM_DOWN_MSG):
        await ask(guard)

    assert guard.reason is DeathReason.UPSTREAM_EXITED


async def test_any_other_upstream_error_propagates_unchanged() -> None:
    error = MCPError(code=-32602, message="Invalid params")
    guard = guard_of(Session(calls=[error]))

    with pytest.raises(MCPError) as seen:
        await ask(guard)

    assert seen.value is error and not guard.dead.is_set()


async def test_the_first_reason_stays() -> None:
    guard = guard_of(Session())
    guard.mark_dead(DeathReason.UPSTREAM_EXITED)
    guard.mark_dead(DeathReason.PROBES_UNANSWERED)

    assert guard.reason is DeathReason.UPSTREAM_EXITED
