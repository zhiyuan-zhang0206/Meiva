# meiva:note
# When a schedule loop sleeps and which slot it claims next, decided from values.
#
# The shell computes each trigger's next slot with `next_fire` from `search_base` and hands the
# result to `plan_step`, which returns either the longest safe sleep or the slot to claim. The
# claim itself, the sleeping and the clock read are the shell's. Every built-in loop is one
# `LoopTiming`: the defaults are the daily loops, and a loop that fires early, rests less or
# sleeps in shorter hops overrides only that field.
# meiva:link schedules/README.md

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

__all__ = ["Fire", "LoopTiming", "Sleep", "Step", "Upcoming", "plan_step", "search_base"]


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
