# meiva:note
# Retry timing of one llm node as values. The node reads its policy once and asks these functions what a failed try means; the clock reading and the jitter roll arrive as arguments.
# meiva:link agent/graph/llm/docs/llm.meiva.okf.md
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RETRY_JITTER_SPAN_S = 10.0
# meiva:note
# The backoff factor the transient schedule was tuned with (every incident validated base 2).
_BACKOFF_FACTOR = 2.0
# meiva:note
# Up to one second of jitter rides on each wait while more than that much budget remains; the last wait reserves it so a sleep cannot drift past the node's budget.
WAIT_JITTER_SPAN_S = 1.0

RetryEnd = Literal["budget_exhausted", "attempts_exhausted"]


# meiva:note
# One try of the node: its 1-based number and when the node's first try started.
@dataclass(frozen=True)
class Attempt:
    number: int
    first_started_at: float

    def elapsed_seconds(self, now: float) -> float:
        return max(0.0, now - self.first_started_at)


# meiva:note
# What bounds one node's retries: the agent's lm config, with the model's try cap resolved.
@dataclass(frozen=True)
class RetryPolicy:
    agent_id: int
    max_attempts: int
    max_total_seconds: float
    initial_interval_seconds: float
    max_interval_seconds: float


# meiva:note
# What one failed try leaves behind: the budget left, and the limit it hit (if any).
@dataclass(frozen=True)
class FailedAttempt:
    remaining_seconds: float
    ended: RetryEnd | None


# meiva:note
# The agent's stable offset in [0, RETRY_JITTER_SPAN_S) of the transient schedule, so agents that failed together do not retry in step.
def retry_phase_jitter(agent_id: int) -> float:
    return RETRY_JITTER_SPAN_S * (agent_id % 1000) / 1000.0


def budget_spent(attempt: Attempt, now: float, policy: RetryPolicy) -> bool:
    return attempt.elapsed_seconds(now) >= policy.max_total_seconds


# meiva:note
# `budget_error` marks the failure that is itself the spent budget (already reported); `fatal` marks a failure no retry follows, which ends on no attempt cap.
def assess_failure(
    attempt: Attempt, now: float, policy: RetryPolicy, *, budget_error: bool, fatal: bool
) -> FailedAttempt:
    remaining = policy.max_total_seconds - attempt.elapsed_seconds(now)
    if remaining <= 0.0 and not budget_error:
        return FailedAttempt(remaining, "budget_exhausted")
    if not fatal and attempt.number >= policy.max_attempts:
        return FailedAttempt(remaining, "attempts_exhausted")
    return FailedAttempt(remaining, None)


# meiva:note
# Seconds to sleep before the next try after the `failed`-th failed one; None ends the node. `remaining_seconds` is the budget the failed try left (None when it recorded none); `jitter_seconds` is a roll in [0, WAIT_JITTER_SPAN_S).
def backoff_wait(
    failed: int, policy: RetryPolicy, *, remaining_seconds: float | None, jitter_seconds: float
) -> float | None:
    if remaining_seconds is not None and remaining_seconds <= 0.0:
        return None
    if failed >= policy.max_attempts:
        return None
    max_interval = policy.max_interval_seconds
    jittered = remaining_seconds is None or remaining_seconds > WAIT_JITTER_SPAN_S
    if remaining_seconds is not None:
        reserve = WAIT_JITTER_SPAN_S if jittered else 0.0
        max_interval = min(max_interval, remaining_seconds - reserve)
    initial = policy.initial_interval_seconds + retry_phase_jitter(policy.agent_id)
    interval = min(max_interval, initial * (_BACKOFF_FACTOR ** (failed - 1)))
    return interval + jitter_seconds if jittered else interval
