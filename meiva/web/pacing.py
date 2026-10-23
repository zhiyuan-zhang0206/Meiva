# meiva:note
# The slot arithmetic of the request pacer, as values. A caller that arrives at `now` with the
# next free slot at `next_at` waits `wait` seconds and leaves the following slot at the returned
# `next_at`. The slot is reserved before the caller sleeps, so a caller that oversleeps starts
# late while the caller behind it keeps its own slot: the observed gap compresses by the
# overshoot but a start never moves earlier than its slot.
# meiva:link meiva/web/docs/providers.meiva.okf.md

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Slot", "reserve"]


@dataclass(frozen=True)
class Slot:
    wait: float
    next_at: float


def reserve(next_at: float, now: float, interval: float) -> Slot:
    return Slot(wait=max(next_at - now, 0.0), next_at=max(now, next_at) + interval)
