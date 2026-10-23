from __future__ import annotations

import pytest

from services.wake.schedule_manager.stall_policy import (
    Signature,
    StallPolicy,
    Watch,
    check_interval_s,
    close_wait_s,
    stall_message,
    stall_note,
    watch_step,
)

POLICY = StallPolicy(timeout_s=100.0)
FRAME: Signature = ("script.py", 7, "fetch")
OTHER: Signature = ("script.py", 9, "store")


def test_the_first_sample_starts_an_observation() -> None:
    watch, stood = watch_step(None, FRAME, 10.0, POLICY)

    assert watch == Watch(FRAME, 10.0)
    assert stood is None


def test_the_same_frame_keeps_its_first_seen_time_until_the_budget_is_spent() -> None:
    watch = Watch(FRAME, 10.0)

    kept, stood = watch_step(watch, FRAME, 109.9, POLICY)
    assert kept == watch and stood is None

    kept, stood = watch_step(watch, FRAME, 110.0, POLICY)
    assert kept == watch and stood == pytest.approx(100.0)


def test_another_frame_restarts_the_observation() -> None:
    watch, stood = watch_step(Watch(FRAME, 10.0), OTHER, 90.0, POLICY)

    assert watch == Watch(OTHER, 90.0)
    assert stood is None


def test_a_parked_sample_resets_the_budget() -> None:
    parked, stood = watch_step(Watch(FRAME, 10.0), None, 50.0, POLICY)
    assert parked == Watch(None, 50.0) and stood is None

    # meiva:note
    # The frame that follows the park is a new observation, not the old one resumed.
    resumed, stood = watch_step(parked, FRAME, 200.0, POLICY)
    assert resumed == Watch(FRAME, 200.0) and stood is None


def test_consecutive_parks_never_stall() -> None:
    watch: Watch | None = None
    for now in (0.0, 500.0, 5000.0):
        watch, stood = watch_step(watch, None, now, POLICY)
        assert stood is None


def test_the_guard_samples_a_quarter_of_the_budget_apart_but_at_most_the_cap() -> None:
    assert check_interval_s(StallPolicy(timeout_s=8.0)) == 2.0
    assert check_interval_s(StallPolicy(timeout_s=1200.0)) == 30.0
    assert check_interval_s(StallPolicy(timeout_s=1200.0, sample_interval_max_s=5.0)) == 5.0


def test_close_waits_for_child_cleanup_and_the_record_deadline() -> None:
    assert close_wait_s(StallPolicy(timeout_s=1.0)) == 19.0
    assert close_wait_s(StallPolicy(timeout_s=1.0, record_deadline_s=1.0)) == 10.0


def test_the_failure_message_names_the_frame() -> None:
    message = stall_message(1234.4, FRAME)

    assert message.startswith("schedule runner stalled 1234s in fetch (script.py:7)")
    assert "hard-exiting" in message


def test_the_run_note_carries_the_budget() -> None:
    assert stall_note(StallPolicy(timeout_s=1200.0)) == "stalled (1200s)"


@pytest.mark.parametrize(
    "durations",
    [
        {"timeout_s": 0.0},
        {"timeout_s": -1.0},
        {"timeout_s": 1.0, "sample_interval_max_s": 0.0},
        {"timeout_s": 1.0, "record_deadline_s": 0.0},
    ],
)
def test_a_policy_with_a_non_positive_duration_is_refused(durations: dict[str, float]) -> None:
    with pytest.raises(ValueError, match="positive"):
        StallPolicy(**durations)
