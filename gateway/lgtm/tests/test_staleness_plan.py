from __future__ import annotations

from gateway.lgtm.staleness_plan import (
    STALENESS_THRESHOLD_S,
    SourceState,
    StalenessEvent,
    check_due,
    staleness_step,
)


def test_a_fresh_heartbeat_with_no_state_is_not_stale_and_says_nothing() -> None:
    step = staleness_step("postgres", None, 30.0, 1000.0)

    assert (step.stale, step.state, step.event) == (False, None, None)


def test_a_missing_heartbeat_enters_stale_once() -> None:
    step = staleness_step("postgres", None, None, 1000.0)

    assert step.stale is True
    assert step.state == SourceState(stale_since=1000.0, last_reported=1000.0)
    assert step.event == StalenessEvent(
        "telemetry_read_stale",
        {
            "source": "postgres",
            "signal": "gateway_latency",
            "threshold_s": 300,
            "age_s": None,
            "action": "entered",
            "reason": "heartbeat missing",
        },
    )


def test_an_old_heartbeat_reads_stale_with_its_age() -> None:
    step = staleness_step("postgres", None, STALENESS_THRESHOLD_S + 1, 1000.0)

    assert step.event is not None
    assert step.event.attributes["reason"] == "heartbeat older than threshold"
    assert step.event.attributes["age_s"] == STALENESS_THRESHOLD_S + 1


def test_a_stale_source_is_quiet_until_a_threshold_has_passed_since_the_last_report() -> None:
    state = SourceState(stale_since=1000.0, last_reported=1000.0)

    quiet = staleness_step("postgres", state, None, 1000.0 + STALENESS_THRESHOLD_S - 1)
    assert (quiet.stale, quiet.state, quiet.event) == (True, state, None)

    loud = staleness_step("postgres", state, None, 1000.0 + STALENESS_THRESHOLD_S)
    assert loud.stale is True
    assert loud.state == SourceState(stale_since=1000.0, last_reported=1000.0 + 300)
    assert loud.event is not None and loud.event.attributes["action"] == "ongoing"


def test_a_fresh_heartbeat_after_stale_recovers_with_the_stale_duration() -> None:
    step = staleness_step("postgres", SourceState(1000.0, 1300.0), 30.0, 1360.0)

    assert (step.stale, step.state) == (False, None)
    assert step.event == StalenessEvent(
        "telemetry_read_recovered",
        {"source": "postgres", "signal": "gateway_latency", "stale_duration_s": 360.0},
    )


def test_a_check_is_due_when_never_run_or_the_interval_has_passed() -> None:
    assert check_due(None, 5.0, 60.0)
    assert not check_due(100.0, 159.9, 60.0)
    assert check_due(100.0, 160.0, 60.0)
    # meiva:note
    # the clock stepped back: ask again
    assert check_due(100.0, 99.0, 60.0)
