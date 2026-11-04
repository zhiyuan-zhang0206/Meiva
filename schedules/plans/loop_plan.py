# meiva:note
# When a schedule loop sleeps and which slot it claims next, decided from values.
#
# The shell computes each trigger's next slot with `next_fire` from `search_base` and hands the
# result to `plan_step`, which returns either the longest safe sleep or the slot to claim. The
# claim itself, the sleeping and the clock read are the shell's. Every built-in loop is one
# `LoopTiming`: the defaults are the daily loops, and a loop that fires early, rests less or
# sleeps in shorter hops overrides only that field. `FailurePolicy` is the other half of one
# loop's shape: what it waits after a round raised, and when that round becomes a report.
# meiva:link schedules/README.md

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

__all__ = [
    "FailurePolicy",
    "Fire",
    "LoopTiming",
    "Sleep",
    "Step",
    "Upcoming",
    "failure_backoff_s",
    "plan_step",
    "search_base",
]


# meiva:note
# The shape of one loop. `tolerance` backs the search for the next slot up from now: cron
# expressions are strictly greater than their base, so a sleep that wakes a fraction of a
# second past the slot would otherwise jump a whole period (the deterministic miss of
# 2026-08-06). `fire_ahead` is how far before its slot a slot may already be claimed; the
# claim is durable and keyed by slot, so claiming early cannot fire twice. `rest` is the pause
# after a claim and `max_sleep` the longest single sleep, so a changed clock is noticed.
@dataclass(frozen=True)
class LoopTiming:
    tolerance: timedelta = timedelta(minutes=2)
    fire_ahead: float = 0.0
    max_sleep: float = 3600.0
    rest: float = 120.0


# meiva:note
# What a loop does with a round that raised: how long it waits before trying the same work again,
# and when one more line in a log stops being enough. The delay doubles from `first_delay_s` and
# stops at `max_delay_s`, so a loop that cannot get past the planning step retries every five
# minutes instead of spinning -- long enough that a persistent cause is not a busy-loop, short
# enough that a schedule with an hourly slot still gets several attempts inside the hour.
# `report_after` counts failed rounds in a row, not elapsed time: a loop that fails twice an hour
# is not the same as one failing every minute, and only the second is worth a person's attention.
@dataclass(frozen=True)
class FailurePolicy:
    first_delay_s: float = 60.0
    max_delay_s: float = 300.0
    report_after: int = 3

    def __post_init__(self) -> None:
        if self.first_delay_s <= 0:
            raise ValueError(f"first_delay_s must be positive, got {self.first_delay_s}")
        if self.max_delay_s < self.first_delay_s:
            raise ValueError(
                f"max_delay_s ({self.max_delay_s}) must not be below first_delay_s "
                f"({self.first_delay_s})"
            )
        if self.report_after < 1:
            raise ValueError(f"report_after must be at least 1, got {self.report_after}")


# meiva:note
# The pause after `failures` failed rounds in a row: `first_delay_s` doubled once per earlier
# failure, never above `max_delay_s`. The doubling is a product over the rounds already failed,
# not a power of the count, because the first failed round waits the first delay, not twice it.
def failure_backoff_s(failures: int, policy: FailurePolicy) -> float:
    if failures < 1:
        raise ValueError(f"failures must be at least 1, got {failures}")
    delay = policy.first_delay_s
    for _ in range(failures - 1):
        delay = min(delay * 2.0, policy.max_delay_s)
    return delay


# meiva:note
# One trigger's next slot (UTC) and the payload its claim carries to the fire callback.
@dataclass(frozen=True)
class Upcoming:
    slot: datetime
    trigger: str | None


@dataclass(frozen=True)
class Sleep:
    seconds: float


# meiva:note
# Claim this slot, then rest `rest` seconds.
@dataclass(frozen=True)
class Fire:
    slot: datetime
    trigger: str | None
    rest: float


Step = Sleep | Fire


# meiva:note
# The time the next-slot search starts from. `floor` is the loop's own memory of what it must
# not fire again: its start, then the latest slot it claimed. A slot at or before the floor
# is never found, so a restart skips what it missed instead of catching up.
def search_base(floor: datetime, now: datetime, timing: LoopTiming) -> datetime:
    _require_aware(floor, "floor")
    _require_aware(now, "now")
    return max(floor, now - timing.tolerance)


# meiva:note
# Claim the earliest slot that is due, trigger order breaking a tie; otherwise sleep until the
# nearest slot, never longer than `max_sleep`.
def plan_step(upcoming: tuple[Upcoming, ...], now: datetime, timing: LoopTiming) -> Step:
    _require_aware(now, "now")
    if not upcoming:
        raise ValueError("a loop needs at least one trigger")
    waits = [(item, (item.slot - now).total_seconds()) for item in upcoming]
    due = [item for item, wait in waits if wait <= timing.fire_ahead]
    if due:
        earliest = min(due, key=lambda item: item.slot)
        return Fire(earliest.slot, earliest.trigger, timing.rest)
    nearest = min(wait for _, wait in waits)
    return Sleep(min(nearest, timing.max_sleep))


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None:
        raise ValueError(f"{name} must carry tzinfo")
