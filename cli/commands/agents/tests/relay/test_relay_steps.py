# meiva:note
# The relay's delivery decisions, as values: what a snapshot, two clock readings and the
# relay's tuning make it do next. No resource is touched; the time of every case is written in
# the case.

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from base.agents.impersonation.status import ImpersonationStatus
from cli.commands.agents.relay.inbox import LeaseRef
from cli.commands.agents.relay.steps import (
    CATCHUP_SECONDS,
    Deliver,
    RelayState,
    RelayTuning,
    Reservation,
    SendStart,
    WaitForWake,
    batch_closed,
    emitted,
    next_step,
    plan_reservation,
    reserved_push,
)
from cli.commands.agents.tests.relay.relay_inputs import (
    LEASE_UUID,
    NOW,
    REF,
    START_MESSAGE,
    TUNING,
    clock,
    message,
    snapshot,
)

STARTED = replace(RelayState.initial(), start_sent=True)


def test_the_start_message_goes_first_and_announces_what_was_pending() -> None:
    step, state = next_step(
        RelayState.initial(),
        snapshot(message(5), message(6)),
        clock(),
        LeaseRef(42, LEASE_UUID),
        TUNING,
    )
    assert step == SendStart(START_MESSAGE)
    assert state.start_sent
    assert state.activation_pending == {5, 6}


def test_a_session_number_adds_the_activation_hint_to_the_briefing() -> None:
    step, _ = next_step(RelayState.initial(), snapshot(), clock(), REF, TUNING)
    assert isinstance(step, SendStart)
    assert step.text.startswith(START_MESSAGE + "\n\n")
    assert "meiva impersonate ack 5 --agent 42" in step.text


def test_empty_start_message_falls_back_to_the_activation_hint() -> None:
    step, _ = next_step(
        RelayState.initial(), snapshot(start_message=""), clock(), LeaseRef(42, LEASE_UUID), TUNING
    )
    assert isinstance(step, SendStart)
    assert step.text.startswith("Meiva control active: agent=42")
    assert f"meiva impersonate ack {LEASE_UUID}" in step.text
    assert f"meiva impersonate inbox {LEASE_UUID}" in step.text
    assert "acknowledge each batch as soon as it arrives" in step.text
    assert "after processing" not in step.text


@pytest.mark.parametrize(
    "status", [s for s in ImpersonationStatus if s is not ImpersonationStatus.ACTIVE]
)
def test_a_lease_that_is_not_active_only_waits_and_sends_nothing(
    status: ImpersonationStatus,
) -> None:
    step, state = next_step(
        RelayState.initial(), snapshot(message(7), status=status), clock(), REF, TUNING
    )
    assert step == WaitForWake(CATCHUP_SECONDS)
    assert state == RelayState.initial()


def test_start_message_then_pending_push_skips_the_merge_window() -> None:
    state = replace(STARTED, activation_pending=frozenset({5, 6}))
    step, after = next_step(
        state, snapshot(message(5), message(6), batch_window=30.0), clock(), REF, TUNING
    )
    assert step == Deliver(0.0)
    assert after.routine_deadline is None


def test_fresh_routine_arrival_waits_out_the_merge_window() -> None:
    page = snapshot(message(8), batch_window=30.0)
    step, state = next_step(STARTED, page, clock(0.0), REF, TUNING)
    assert step == WaitForWake(30.0)
    assert state.routine_deadline == 30.0

    step, state = next_step(state, page, clock(29.0), REF, TUNING)
    assert step == WaitForWake(1.0)
    assert state.routine_deadline == 30.0

    step, state = next_step(state, page, clock(30.0), REF, TUNING)
    assert step == Deliver(0.0)
    assert state.routine_deadline is None


def test_the_window_never_holds_a_wake_past_the_leases_lifetime() -> None:
    page = snapshot(message(8), batch_window=30.0, lifetime_s=8.0)
    step, _ = next_step(STARTED, page, clock(), REF, TUNING)
    assert step == WaitForWake(8.0)


@pytest.mark.parametrize(
    ("kind", "source"), [("chat", "user"), ("cancel", "system"), ("reminder", "system")]
)
def test_urgent_arrivals_push_immediately_without_the_merge_window(kind: str, source: str) -> None:
    page = snapshot(message(9, kind=kind, source=source), batch_window=30.0)
    step, state = next_step(STARTED, page, clock(), REF, TUNING)
    assert step == Deliver(0.0)
    assert state.routine_deadline is None


def test_one_urgent_row_releases_the_whole_page() -> None:
    page = snapshot(message(8), message(9, kind="chat", source="user"), batch_window=30.0)
    step, _ = next_step(STARTED, page, clock(), REF, TUNING)
    assert step == Deliver(0.0)


def test_a_zero_window_delivers_routine_rows_at_once() -> None:
    step, _ = next_step(STARTED, snapshot(message(8), batch_window=0.0), clock(), REF, TUNING)
    assert step == Deliver(0.0)


def test_a_redelivery_skips_the_merge_window() -> None:
    page = snapshot(message(11, attempts=1, due=True), batch_window=30.0)
    step, _ = next_step(STARTED, page, clock(), REF, TUNING)
    assert step == Deliver(0.0)


def test_rows_inside_their_ack_window_or_out_of_budget_are_not_due() -> None:
    page = snapshot(
        message(11, attempts=1, due=False),
        message(12, attempts=2, due=True),
        max_attempts=2,
    )
    step, state = next_step(STARTED, page, clock(), REF, TUNING)
    assert step == WaitForWake(CATCHUP_SECONDS)
    assert state == STARTED


def test_emits_respect_the_monitor_event_rate_across_ack_cycles() -> None:
    paced = replace(TUNING, min_emit_interval=2.0)
    state = emitted(STARTED, at=10.0)
    step, _ = next_step(state, snapshot(message(3)), clock(10.1), REF, paced)
    assert isinstance(step, Deliver)
    assert step.pause == pytest.approx(1.9)

    step, _ = next_step(state, snapshot(message(3)), clock(20.0), REF, paced)
    assert step == Deliver(0.0)

    debounced = replace(paced, debounce=5.0)
    step, _ = next_step(state, snapshot(message(3)), clock(10.1), REF, debounced)
    assert step == Deliver(5.0)


def test_wait_never_extends_beyond_lease_lifetime() -> None:
    step, _ = next_step(STARTED, snapshot(lifetime_s=8.0), clock(), REF, TUNING)
    assert step == WaitForWake(8.0)


def test_local_clock_cannot_revoke_db_active_lease_or_busy_spin() -> None:
    expired_by_the_local_clock = snapshot(lifetime_s=-300.0)
    step, _ = next_step(STARTED, expired_by_the_local_clock, clock(), REF, TUNING)
    assert step == WaitForWake(0.5)


def test_the_catchup_bound_applies_when_the_lease_has_long_to_live() -> None:
    short = replace(TUNING, catchup_seconds=7.0)
    step, _ = next_step(STARTED, snapshot(lifetime_s=3600.0), clock(), REF, short)
    assert step == WaitForWake(7.0)


def test_a_lease_expiry_without_a_timezone_is_refused() -> None:
    naive = replace(snapshot(), expires_at=(NOW + timedelta(minutes=5)).replace(tzinfo=None))
    with pytest.raises(ValueError, match="timezone"):
        next_step(STARTED, naive, clock(), REF, TUNING)


def test_a_push_resets_the_pacing_and_a_pass_resets_the_window() -> None:
    state = replace(STARTED, routine_deadline=30.0)
    assert emitted(state, at=4.0).last_emit == 4.0
    assert batch_closed(state).routine_deadline is None
    assert batch_closed(state).start_sent


def test_a_reservation_covers_only_due_rows_in_id_order() -> None:
    page = snapshot(message(12), message(11), message(13, attempts=2), max_attempts=2)
    assert plan_reservation(page) == Reservation((11, 12), redelivery=False)


def test_retries_precede_fresh_rows_so_arrivals_cannot_starve_them() -> None:
    page = snapshot(message(11, attempts=1), message(12), message(13, attempts=1))
    assert plan_reservation(page) == Reservation((11, 13), redelivery=True)


def test_nothing_due_reserves_nothing() -> None:
    assert plan_reservation(snapshot(message(11, attempts=1, due=False))) == Reservation(
        (), redelivery=False
    )


def test_redelivery_covers_only_still_unacknowledged_ids() -> None:
    page = snapshot(message(11, attempts=1), message(12, attempts=1))
    push = reserved_push(page, frozenset({12}), redelivery=True, ref=REF, tuning=TUNING)
    assert "ids=12" in push
    assert "[id=11]" not in push
    assert "(re-delivery: unacknowledged)" in push


def test_a_first_push_is_not_marked_as_a_redelivery() -> None:
    page = snapshot(message(5), message(6))
    push = reserved_push(page, frozenset({5, 6}), redelivery=False, ref=REF, tuning=TUNING)
    assert "ids=5,6" in push
    assert "re-delivery" not in push


def test_a_long_body_is_cut_but_the_ack_line_stays() -> None:
    page = snapshot(message(1, content="x" * 5000))
    push = reserved_push(
        page, frozenset({1}), redelivery=False, ref=REF, tuning=replace(TUNING, max_chars=2000)
    )
    assert "truncated" in push
    assert "before acknowledging" in push
    assert "ACK after receiving this message: meiva impersonate ack 5 1 --agent 42" in push
    body = next(line for line in push.splitlines() if line.startswith("x" * 10))
    assert len(body) == 2000 + len(
        " (truncated — run the inbox command to read the full body before acknowledging)"
    )


def test_a_reservation_for_a_row_the_snapshot_lacks_is_refused() -> None:
    with pytest.raises(ValueError, match="not in this snapshot"):
        reserved_push(
            snapshot(message(1)), frozenset({2}), redelivery=False, ref=REF, tuning=TUNING
        )


@pytest.mark.parametrize("debounce", [-1.0, 31.0, float("nan"), float("inf")])
def test_invalid_debounce_fails_before_open(debounce: float) -> None:
    with pytest.raises(ValueError, match="debounce"):
        RelayTuning(debounce=debounce, max_chars=None)


@pytest.mark.parametrize("catchup", [0.0, -1.0, float("nan"), float("inf")])
def test_a_catchup_that_cannot_repair_missed_wakes_is_refused(catchup: float) -> None:
    with pytest.raises(ValueError, match="catchup_seconds"):
        RelayTuning(debounce=0.0, max_chars=None, catchup_seconds=catchup)
