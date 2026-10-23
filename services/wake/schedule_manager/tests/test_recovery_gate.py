from __future__ import annotations

import pytest

from services.wake.schedule_manager.recovery_gate import GateState, GateStep, observe_gate, refuse

ALERT_AFTER_S = 60.0


def _observe(state: GateState, *, permitted: bool, now: float) -> GateStep:
    return observe_gate(state, permitted=permitted, now=now, alert_after_s=ALERT_AFTER_S)


def test_a_short_gate_reports_nothing() -> None:
    first = _observe(GateState(), permitted=False, now=1000.0)
    second = _observe(first.state, permitted=False, now=1010.0)

    assert first.held_for_s is None
    assert second.held_for_s is None


def test_a_sustained_gate_reports_once_per_episode() -> None:
    state = _observe(GateState(), permitted=False, now=1000.0).state
    alerted = _observe(state, permitted=False, now=1061.0)
    later = _observe(alerted.state, permitted=False, now=1661.0)

    assert alerted.held_for_s == pytest.approx(61.0)
    assert later.held_for_s is None


def test_the_next_episode_alerts_again() -> None:
    state = _observe(GateState(), permitted=False, now=1000.0).state
    state = _observe(state, permitted=False, now=1061.0).state
    state = _observe(state, permitted=True, now=1062.0).state
    state = _observe(state, permitted=False, now=1063.0).state
    again = _observe(state, permitted=False, now=1124.0)

    assert again.held_for_s == pytest.approx(61.0)


def test_a_refusal_is_reported_once_per_schedule_per_episode() -> None:
    state = _observe(GateState(), permitted=False, now=1000.0).state

    state, first = refuse(state, 7)
    state, repeated = refuse(state, 7)
    state, other = refuse(state, 9)

    assert (first, repeated, other) == (True, False, True)


def test_lifting_the_gate_forgets_the_previous_episode() -> None:
    state = _observe(GateState(), permitted=False, now=1000.0).state
    state, first = refuse(state, 7)
    state = _observe(state, permitted=True, now=1001.0).state
    state = _observe(state, permitted=False, now=1002.0).state
    state, second = refuse(state, 7)

    assert (first, second) == (True, True)


def test_a_permitted_receipt_closes_the_gate_at_once() -> None:
    state, _ = refuse(_observe(GateState(), permitted=False, now=1000.0).state, 7)

    assert _observe(state, permitted=True, now=1001.0).state == GateState()
