# meiva:note
# What a native LGTM archive download does between attempts.
#
# One download holds a single absolute deadline. These functions turn the elapsed time and the
# attempt number into the next wait; the shell in `lgtm_native.py` owns the clock, the socket and
# the file, and `download_report.py` writes what the operator reads.

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "AttemptState",
    "DownloadLimits",
    "content_length",
    "progress_due",
    "retry_wait",
]


# meiva:note
# The bounds of one download: the whole-operation budget, the per-request socket timeout, the
# attempt count, the linear backoff step and how often progress is printed.
@dataclass(frozen=True)
class DownloadLimits:
    total_budget_s: float
    socket_timeout_s: float
    attempts: int
    retry_backoff_s: float
    progress_interval_s: float


# meiva:note
# Seconds to wait before the next attempt, or None when `attempt` (1-based) was the last one.
#
# The backoff grows by one step per attempt and never runs past the budget that remains, so a
# spent budget waits zero seconds and the next attempt fails on its own budget check.
def retry_wait(limits: DownloadLimits, *, attempt: int, remaining_s: float) -> float | None:
    if attempt >= limits.attempts:
        return None
    return min(limits.retry_backoff_s * attempt, max(remaining_s, 0.0))


# meiva:note
# The body size the server announced, or None when the header is absent as a number.
#
# A missing header reads as 0, which the progress line treats as "unknown".
def content_length(header: str | None) -> int | None:
    try:
        return int(header or 0)
    except ValueError:
        return None


# meiva:note
# Whether the heartbeat is due: at least one interval passed since the last report.
def progress_due(limits: DownloadLimits, *, elapsed_s: float, last_report_s: float) -> bool:
    return elapsed_s - last_report_s >= limits.progress_interval_s


# meiva:note
# Where one download stands when an attempt fails: the 1-based attempt number, the seconds used,
# the seconds of budget left and the bytes the attempt received.
@dataclass(frozen=True)
class AttemptState:
    attempt: int
    used_s: float
    remaining_s: float
    received: int
