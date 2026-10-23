# meiva:note
# How long a failing convergence is retried and how often its failure is logged: values in.
#
# A reap that survives, or a live session whose launch provenance cannot be read, is retried for
# at most ``BLOCKED_GIVE_UP_AFTER_S`` of one failure streak; two failures further apart than
# ``BLOCKED_STREAK_GAP_S`` (a stopped or paused service sits between them) are not one streak. The
# manager holds the per-schedule state and reads the clock once per tick; these functions turn
# the clock reading and the previous state into the next state and what to do about it.

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "BLOCKED_GIVE_UP_AFTER_S",
    "BLOCKED_STREAK_GAP_S",
    "CLEANUP_FAILURE_LOG_INTERVAL_S",
    "HOST_UNKNOWN_LOG_INTERVAL_S",
    "ReapLog",
    "Streak",
    "give_up_message",
    "host_unknown_due",
    "note_blocked",
    "reap_log_step",
]

BLOCKED_GIVE_UP_AFTER_S = 600.0
BLOCKED_STREAK_GAP_S = 60.0
CLEANUP_FAILURE_LOG_INTERVAL_S = 60.0
HOST_UNKNOWN_LOG_INTERVAL_S = 300.0


# meiva:note
# The first and the latest clock reading of one run of failed attempts.
@dataclass(frozen=True)
class Streak:
    first: float
    latest: float


# meiva:note
# When a schedule's reap failure was last logged and how many repeats were held back.
@dataclass(frozen=True)
class ReapLog:
    last_logged: float
    suppressed: int


# meiva:note
# Count one failed attempt at ``now``.
#
# The second element is how long the streak has lasted when it has reached the give-up
# age, otherwise None.
def note_blocked(streak: Streak | None, now: float) -> tuple[Streak, float | None]:
    if streak is None or now - streak.latest > BLOCKED_STREAK_GAP_S:
        streak = Streak(first=now, latest=now)
    else:
        streak = Streak(first=streak.first, latest=now)
    blocked_s = now - streak.first
    return streak, (blocked_s if blocked_s >= BLOCKED_GIVE_UP_AFTER_S else None)


# meiva:note
# Log an unresolved reap at most once per schedule per cooldown.
#
# The second element is the number of repeats held back since the last log when this one is to
# be logged, or None while the cooldown still holds.
def reap_log_step(prior: ReapLog | None, now: float) -> tuple[ReapLog, int | None]:
    if prior is not None and now - prior.last_logged < CLEANUP_FAILURE_LOG_INTERVAL_S:
        return ReapLog(prior.last_logged, prior.suppressed + 1), None
    return ReapLog(now, 0), 0 if prior is None else prior.suppressed


# meiva:note
# Whether an unanswering session host has gone unmentioned long enough to log again.
def host_unknown_due(last_logged: float, now: float) -> bool:
    return now - last_logged >= HOST_UNKNOWN_LOG_INTERVAL_S


# meiva:note
# The ``last_error`` of a schedule the manager stopped retrying.
def give_up_message(why: str, blocked_s: float) -> str:
    return (
        f"Convergence gave up after {blocked_s:.0f}s: {why}; inspect the process, "
        "stop it by hand if needed, then explicitly restart"
    )
