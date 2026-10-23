# meiva:note
# When a schedule runner counts as stalled: values in, verdicts out.
#
# The stall guard (``engine.StallGuard``) samples the runner's main-thread frame on a thread
# and acts on a verdict. Everything it decides is here as a function of the policy, the last
# observation and the clock reading, so a test passes numbers and asserts the verdict:
#
# - how often the guard samples,
# - whether a sample extends the current observation or starts a new one, and when the
#   observation has stood still for the whole timeout,
# - the failure message and the run-history note a stall leaves,
# - how long ``StallGuard.close`` waits for an admitted action.

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "Signature",
    "StallPolicy",
    "Watch",
    "check_interval_s",
    "close_wait_s",
    "stall_message",
    "stall_note",
    "watch_step",
]

# meiva:note
# Source file, line and function of the deepest frame a sample found.
Signature = tuple[str, int, str]

# meiva:note
# What ``close`` waits for an admitted action besides its record deadline: child cleanup
# (3s TERM grace + 5s reap) and 1s margin.
_CLEANUP_WAIT_S = 9.0


# meiva:note
# The stall budget of one runner process.
#
# ``timeout_s`` is how long the main thread may stand on one non-park frame, and the
# wall-clock bound of a non-``.py`` command. The guard samples a quarter of it apart, at
# most ``sample_interval_max_s`` apart. Both failure writes together get
# ``record_deadline_s`` after child cleanup: a database that is the reason for the stall
# cannot keep the runner alive past it.
@dataclass(frozen=True)
class StallPolicy:
    timeout_s: float
    sample_interval_max_s: float = 30.0
    record_deadline_s: float = 10.0

    def __post_init__(self) -> None:
        if not (
            self.timeout_s > 0 and self.sample_interval_max_s > 0 and self.record_deadline_s > 0
        ):
            raise ValueError("every stall policy duration must be positive")


# meiva:note
# The frame the guard last saw and the clock reading it was first seen at.
@dataclass(frozen=True)
class Watch:
    signature: Signature | None
    since: float


# meiva:note
# How long the guard waits between samples.
def check_interval_s(policy: StallPolicy) -> float:
    return min(policy.sample_interval_max_s, policy.timeout_s / 4)


# meiva:note
# How long ``StallGuard.close`` waits for an action that was already admitted.
def close_wait_s(policy: StallPolicy) -> float:
    return _CLEANUP_WAIT_S + policy.record_deadline_s


# meiva:note
# Fold one sample into the observation.
#
# A sample with no frame, a parked frame (``signature`` None) or a frame other than the
# last one starts a new observation at ``now``; leaving a park resets the budget. The
# second element is how long the same frame has stood when that reaches the timeout,
# otherwise None.
def watch_step(
    previous: Watch | None, signature: Signature | None, now: float, policy: StallPolicy
) -> tuple[Watch, float | None]:
    if previous is None or signature is None or signature != previous.signature:
        return Watch(signature, now), None
    stood = now - previous.since
    return previous, (stood if stood >= policy.timeout_s else None)


# meiva:note
# The ``last_error`` a stall leaves.
def stall_message(stood_s: float, signature: Signature) -> str:
    filename, lineno, function = signature
    return (
        f"schedule runner stalled {stood_s:.0f}s in {function} ({filename}:{lineno}) — "
        "hard-exiting; check the gateway / DB / network the script calls into"
    )


# meiva:note
# The run-history note a stall leaves.
def stall_note(policy: StallPolicy) -> str:
    return f"stalled ({policy.timeout_s:.0f}s)"
