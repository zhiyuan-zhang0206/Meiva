from __future__ import annotations

from services.wake.schedule_manager.blocked_streak import (
    BLOCKED_GIVE_UP_AFTER_S,
    BLOCKED_STREAK_GAP_S,
    CLEANUP_FAILURE_LOG_INTERVAL_S,
    HOST_UNKNOWN_LOG_INTERVAL_S,
    ReapLog,
    Streak,
    give_up_message,
    host_unknown_due,
    note_blocked,
    reap_log_step,
)


def test_the_first_failure_starts_a_streak() -> None:
    streak, blocked_s = note_blocked(None, 1000.0)

    assert streak == Streak(first=1000.0, latest=1000.0)
    assert blocked_s is None


def test_failures_inside_the_gap_extend_one_streak_until_the_give_up_age() -> None:
    streak, _ = note_blocked(None, 1000.0)
    now = 1000.0
    while now - 1000.0 < BLOCKED_GIVE_UP_AFTER_S - 30:
        now += 30
        streak, blocked_s = note_blocked(streak, now)
        assert blocked_s is None
    streak, blocked_s = note_blocked(streak, now + 30)

    assert blocked_s == BLOCKED_GIVE_UP_AFTER_S
    assert streak.first == 1000.0


def test_a_gap_longer_than_the_streak_gap_starts_a_new_streak() -> None:
    streak = Streak(first=1000.0, latest=1000.0)

    for index in range(4):
        streak, blocked_s = note_blocked(streak, streak.latest + BLOCKED_STREAK_GAP_S + 1)
        assert blocked_s is None
        assert streak.first == streak.latest, f"failure {index} continued a stale streak"


def test_the_first_reap_failure_is_logged_without_a_summary() -> None:
    state, held_back = reap_log_step(None, 1000.0)

    assert state == ReapLog(last_logged=1000.0, suppressed=0)
    assert held_back == 0


def test_repeats_inside_the_cooldown_are_held_back_and_counted() -> None:
    state, _ = reap_log_step(None, 1000.0)
    state, held_back = reap_log_step(state, 1000.0 + CLEANUP_FAILURE_LOG_INTERVAL_S - 1)

    assert held_back is None
    assert state == ReapLog(last_logged=1000.0, suppressed=1)


def test_the_next_log_after_the_cooldown_reports_how_many_were_held_back() -> None:
    state, _ = reap_log_step(None, 1000.0)
    state, _ = reap_log_step(state, 1010.0)
    state, _ = reap_log_step(state, 1020.0)

    state, held_back = reap_log_step(state, 1000.0 + CLEANUP_FAILURE_LOG_INTERVAL_S)

    assert held_back == 2
    assert state == ReapLog(last_logged=1000.0 + CLEANUP_FAILURE_LOG_INTERVAL_S, suppressed=0)


def test_an_unanswering_host_is_mentioned_again_only_after_its_interval() -> None:
    assert host_unknown_due(1000.0 - HOST_UNKNOWN_LOG_INTERVAL_S, 1000.0)
    assert not host_unknown_due(1000.0 - HOST_UNKNOWN_LOG_INTERVAL_S + 1, 1000.0)


def test_the_give_up_message_names_the_cause_and_the_age() -> None:
    message = give_up_message("the session x survived its reap", 630.4)

    assert message.startswith("Convergence gave up after 630s: the session x survived its reap")
    assert "explicitly restart" in message
