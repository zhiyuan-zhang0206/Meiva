# meiva:note
# The pacer promises a schedule, not an observable spacing. `reserve` fixes a caller's slot
# before the caller sleeps, so a caller that oversleeps by epsilon starts late and the caller
# behind it keeps the slot it had already reserved: the observed gap shrinks by epsilon while
# the slot never moves. Overshooting can therefore only make a request later than planned,
# never earlier. This proves reserved slots, not minimum gaps between actual network receipts.

from __future__ import annotations

import itertools

import pytest

from meiva.web.pacing import reserve

_INTERVAL = 0.2


# meiva:note
# Step `count` callers through the arithmetic on a clock that advances by exactly each wait,
# plus `overshoot` on the first wait only: one late wake-up, since letting every wait overshoot
# would hide the compression it causes.
def _paced_starts(count: int, *, overshoot: float = 0.0) -> tuple[list[float], list[float]]:
    now = 1_000.0
    next_at = 0.0
    waits: list[float] = []
    starts: list[float] = []
    for _ in range(count):
        slot = reserve(next_at, now, _INTERVAL)
        next_at = slot.next_at
        if slot.wait > 0:
            extra = overshoot if not waits else 0.0
            waits.append(slot.wait)
            now += slot.wait + extra
        starts.append(now)
    return waits, starts


def test_the_pacer_reserves_slots_one_interval_apart() -> None:
    waits, starts = _paced_starts(4)

    assert waits == pytest.approx([0.2, 0.2, 0.2])
    assert starts == sorted(starts)
    assert len(set(starts)) == 4
    assert [later - earlier for earlier, later in itertools.pairwise(starts)] == pytest.approx(
        [0.2, 0.2, 0.2]
    )


# meiva:note
# 50 ms of overshoot on one wait: the next wait is 0.15 instead of 0.20 (the slot it reserved
# was fixed before this caller woke), so the observed gap compresses by exactly the overshoot
# while the requested slot sequence stays 0.20 apart, and no start moves earlier than its slot.
def test_an_oversleep_compresses_the_gap_without_moving_a_slot() -> None:
    waits, starts = _paced_starts(3, overshoot=0.05)

    assert waits == pytest.approx([0.2, 0.15])
    assert starts[1] - starts[0] == pytest.approx(0.25)
    assert starts[2] - starts[1] == pytest.approx(0.15)
    assert starts == sorted(starts)


def test_a_caller_after_an_idle_stretch_takes_the_slot_it_arrived_at() -> None:
    slot = reserve(10.0, 50.0, _INTERVAL)

    assert slot.wait == 0.0
    assert slot.next_at == pytest.approx(50.2)
