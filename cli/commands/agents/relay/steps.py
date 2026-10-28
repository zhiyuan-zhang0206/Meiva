# meiva:note
# What one relay does next, as values: the delivery loop's decisions.
#
# The shell reads a snapshot and takes two clock readings, asks `next_step` what to do, does
# it, and asks again. Everything that depends on time is a function of those readings: the
# merge window that holds a burst of routine rows, the rate limit between pushes, and how long
# the shell may wait for a wake. Database time and the durable reservations own the ACK
# windows and the delivery budget (`InboxMessage.delivery_due` and `delivery_attempts`); the
# local monotonic clock only batches and rate-limits.
#
# A failed or ambiguous host submission spends an attempt, never an ACK, and a reservation is
# taken immediately before the host call so an ACK or release racing the read is caught.
# meiva:link docs/conventions/agents/agent-impersonation-hosts.md

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime

from base.clock import Timers
from cli.commands.agents.relay.inbox import InboxSnapshot, LeaseRef, due_ids
from cli.commands.agents.relay.push_text import activation_hint, message_push

__all__ = [
    "CATCHUP_SECONDS",
    "PUSH_MAX_CHARS",
    "ClockReading",
    "ConnectionRetry",
    "Deliver",
    "RelayState",
    "RelayTuning",
    "Reservation",
    "SendStart",
    "Step",
    "WaitForWake",
    "batch_closed",
    "connection_retry_delay",
    "emitted",
    "next_step",
    "plan_reservation",
    "reserved_push",
]

CATCHUP_SECONDS = 30.0
MIN_EMIT_INTERVAL_SECONDS = 2.0
MIN_WAKE_SECONDS = 0.5
PUSH_MAX_CHARS = 2000


# meiva:note
# The knobs one relay was started with. `debounce` coalesces a burst of wakes before a push;
# `catchup_seconds` bounds a wait so a missed Redis wake is repaired by a periodic re-read;
# `min_emit_interval` is the pace Claude Monitor replenishes one event allowance at;
# `min_wake_seconds` is the shortest wait, so a lease the local clock thinks expired but the
# database still holds active cannot busy-spin the relay. `max_chars` bounds each pushed body
# (None leaves it whole). The three defaults are today's constants.
@dataclass(frozen=True)
class RelayTuning:
    debounce: float
    max_chars: int | None
    catchup_seconds: float = CATCHUP_SECONDS
    min_emit_interval: float = MIN_EMIT_INTERVAL_SECONDS
    min_wake_seconds: float = MIN_WAKE_SECONDS

    def __post_init__(self) -> None:
        if not math.isfinite(self.debounce) or not 0 <= self.debounce <= CATCHUP_SECONDS:
            raise ValueError(f"debounce must be between 0 and {CATCHUP_SECONDS:g} seconds")
        if not math.isfinite(self.catchup_seconds) or self.catchup_seconds <= 0:
            raise ValueError("catchup_seconds must be finite and positive")
        if not math.isfinite(self.min_emit_interval) or self.min_emit_interval < 0:
            raise ValueError("min_emit_interval must be finite and nonnegative")
        if not math.isfinite(self.min_wake_seconds) or self.min_wake_seconds < 0:
            raise ValueError("min_wake_seconds must be finite and nonnegative")
        if self.max_chars is not None and self.max_chars <= 0:
            raise ValueError("max_chars must be positive")


# meiva:note
# How long this relay waits between attempts at a database connection the pool could not
# provide, and the timers it waits on and measures the outage with. `delays` is the whole
# policy: one entry per retry, so the budget is their sum. The timers are a value, so a test
# supplies `ManualTimers` and never waits on the wall clock, and nothing here reads one itself.
# It bounds connection trouble only — the lease's own fences are re-read on every attempt and
# are never cached across an outage.
@dataclass(frozen=True)
class ConnectionRetry:
    delays: tuple[float, ...]
    timers: Timers

    def __post_init__(self) -> None:
        if any(not math.isfinite(delay) or delay < 0 for delay in self.delays):
            raise ValueError("retry delays must be finite and nonnegative")


# meiva:note
# The wait before retry `attempt` (1 is the first retry), or None once the policy is spent.
def connection_retry_delay(attempt: int, delays: tuple[float, ...]) -> float | None:
    return delays[attempt - 1] if 1 <= attempt <= len(delays) else None


# meiva:note
# The two clock readings one decision uses: the loop's monotonic seconds for batching and
# pacing, and the wall instant against which the lease's database expiry is bounded.
@dataclass(frozen=True)
class ClockReading:
    monotonic_now: float
    wall_now: datetime


# meiva:note
# What the relay remembers between steps: whether the start message went out, when the last
# push did, the open merge window's deadline, and the ids pending when the start went out
# (their first push skips the merge window, since the start message already announced them).
@dataclass(frozen=True)
class RelayState:
    start_sent: bool
    last_emit: float
    routine_deadline: float | None
    activation_pending: frozenset[int]

    @classmethod
    def initial(cls) -> RelayState:
        return cls(
            start_sent=False,
            last_emit=float("-inf"),
            routine_deadline=None,
            activation_pending=frozenset(),
        )


# meiva:note
# Push the session's start message, then read again.
@dataclass(frozen=True)
class SendStart:
    text: str


# meiva:note
# Wait for a Redis wake for at most this many seconds, then read again.
@dataclass(frozen=True)
class WaitForWake:
    timeout: float


# meiva:note
# Pause this long (debounce and rate limit), read a fresh snapshot, then reserve and push
# what is due in it.
@dataclass(frozen=True)
class Deliver:
    pause: float


Step = SendStart | WaitForWake | Deliver


# meiva:note
# The ids to reserve from a fresh snapshot, and whether they are redeliveries (rows already
# pushed whose ACK window ran out, which precede fresh rows).
@dataclass(frozen=True)
class Reservation:
    ids: tuple[int, ...]
    redelivery: bool


def _seconds_left(snapshot: InboxSnapshot, clock: ClockReading) -> float:
    if snapshot.expires_at.tzinfo is None:
        raise ValueError("Lease expiry must include a timezone")
    return (snapshot.expires_at - clock.wall_now).total_seconds()


def _wake_timeout(
    snapshot: InboxSnapshot, clock: ClockReading, tuning: RelayTuning, window_left: float | None
) -> float:
    bound = min(tuning.catchup_seconds, _seconds_left(snapshot, clock))
    if window_left is not None:
        bound = min(bound, window_left)
    return max(tuning.min_wake_seconds, bound)


def _start_text(snapshot: InboxSnapshot, ref: LeaseRef) -> str:
    hint = activation_hint(ref, snapshot.budget)
    if not snapshot.start_message:
        return hint
    if isinstance(ref.lease_ref, int):
        return snapshot.start_message + "\n\n" + hint
    return snapshot.start_message


# meiva:note
# (deadline, remaining) for a routine-only page still inside its merge window.
#
# Returns (None, None) when the page is not window-eligible (no new ids, any urgent id, or a
# disabled window of 0) or when the window has already elapsed. `deadline` carries an open
# window forward across polls so a burst keeps one shared deadline.
def _window_wait(
    snapshot: InboxSnapshot, new_ids: frozenset[int], routine_deadline: float | None, now: float
) -> tuple[float | None, float | None]:
    if not new_ids or new_ids - snapshot.routine_ids or snapshot.batch_window <= 0:
        return None, None
    if routine_deadline is None:
        routine_deadline = now + snapshot.batch_window
    remaining = routine_deadline - now
    if remaining <= 0:
        return None, None
    return routine_deadline, remaining


# meiva:note
# The next step for an open or pending lease, and the state after taking it.
#
# A lease that is not active only waits (the database owns acceptance, rejection and expiry).
# The start message goes first, even to an empty inbox. Due rows are held while their merge
# window is open, unless they are redeliveries, urgent, or were pending at the start; then
# the relay paces itself to the debounce and the rate limit before it delivers. With nothing
# due it waits, bounded by the lease's lifetime so it never outlives the database's expiry.
def next_step(
    state: RelayState,
    snapshot: InboxSnapshot,
    clock: ClockReading,
    ref: LeaseRef,
    tuning: RelayTuning,
) -> tuple[Step, RelayState]:
    if not snapshot.active:
        return WaitForWake(_wake_timeout(snapshot, clock, tuning, None)), state
    if not state.start_sent:
        started = replace(state, start_sent=True, activation_pending=snapshot.message_ids)
        return SendStart(_start_text(snapshot, ref)), started
    redelivery = bool(due_ids(snapshot, redelivery=True))
    due = due_ids(snapshot, redelivery=redelivery)
    if not due:
        return WaitForWake(_wake_timeout(snapshot, clock, tuning, None)), state
    if not redelivery and not due & state.activation_pending:
        deadline, remaining = _window_wait(
            snapshot, due, state.routine_deadline, clock.monotonic_now
        )
        state = replace(state, routine_deadline=deadline)
        if remaining is not None:
            return WaitForWake(_wake_timeout(snapshot, clock, tuning, remaining)), state
    pause = max(tuning.debounce, state.last_emit + tuning.min_emit_interval - clock.monotonic_now)
    return Deliver(pause), state


# meiva:note
# The state after a push the host accepted at this monotonic instant.
def emitted(state: RelayState, *, at: float) -> RelayState:
    return replace(state, last_emit=at)


# meiva:note
# The state once a delivery pass is over: the merge window is spent either way.
def batch_closed(state: RelayState) -> RelayState:
    return replace(state, routine_deadline=None)


# meiva:note
# What to reserve from a snapshot read after the pause. Redeliveries first: if any row's
# retry is due, only retries are reserved, so fresh arrivals cannot starve them.
def plan_reservation(snapshot: InboxSnapshot) -> Reservation:
    redelivery = bool(due_ids(snapshot, redelivery=True))
    return Reservation(tuple(sorted(due_ids(snapshot, redelivery=redelivery))), redelivery)


# meiva:note
# The envelope for the rows a reservation granted, in id order.
def reserved_push(
    snapshot: InboxSnapshot,
    reserved: frozenset[int],
    *,
    redelivery: bool,
    ref: LeaseRef,
    tuning: RelayTuning,
) -> str:
    return message_push(
        ref,
        [snapshot.message(i) for i in sorted(reserved)],
        budget=snapshot.budget,
        redelivery=redelivery,
        max_chars=tuning.max_chars,
    )
