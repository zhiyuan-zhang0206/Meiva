# meiva:note
# When the database write path counts as stalled, decided from PgBouncer's per-minute stats.
#
# Over 29 days of history (40,375 minutes) the rule below marked only the two real incidents and
# no quiet night: three consecutive minutes under 20 xacts/s with an average query time above
# 300 ms. It is the root-cause signal an agent-host crash wave follows. `plan_poll` takes the
# state the watcher carries and the stats lines it has not seen, and returns the new state, what
# to print and the alert to send; the cooldown keeps one incident from paging every minute.
# meiva:link schedules/README.md

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = ["PollPlan", "StallSample", "WatchState", "parse_stats", "plan_poll"]

_WINDOW = 3
_MIN_XACTS = 20
_MAX_QUERY_US = 300_000
_COOLDOWN_SECONDS = 1800.0
_MEMORY = 12
_STATS = (
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+) CST \[\d+\] LOG stats: "
    r"(\d+) xacts/s, (\d+) queries/s,.*?, xact \d+ us, query (\d+) us, wait (\d+) us"
)


@dataclass(frozen=True)
class StallSample:
    stamp: str
    xacts_per_s: int
    queries_per_s: int
    query_us: int


# meiva:note
# What one watcher carries between polls: the newest stamp it has processed, its last
# `_MEMORY` samples, when it last alerted, and whether an incident is open.
@dataclass(frozen=True)
class WatchState:
    seen: str | None
    window: tuple[StallSample, ...]
    last_alert: float
    alerted: bool


# meiva:note
# `log_lines` carry no timestamp (the shell stamps them in the cluster zone) and `alert` is the
# text to send to the operators, or None. `advanced` says whether any new sample arrived, so the
# shell rewrites its state file only when something changed.
@dataclass(frozen=True)
class PollPlan:
    state: WatchState
    log_lines: tuple[str, ...]
    alert: str | None
    advanced: bool


def parse_stats(lines: Sequence[str]) -> tuple[StallSample, ...]:
    samples: list[StallSample] = []
    for line in lines:
        match = re.match(_STATS, line)
        if match is not None:
            samples.append(StallSample(match[1], int(match[2]), int(match[3]), int(match[4])))
    return tuple(samples)


def _stalled(sample: StallSample) -> bool:
    return sample.xacts_per_s < _MIN_XACTS and sample.query_us > _MAX_QUERY_US


def _confirmed(window: Sequence[StallSample]) -> bool:
    return len(window) >= _WINDOW and all(_stalled(item) for item in window[-_WINDOW:])


def _describe(samples: Sequence[StallSample]) -> str:
    return "; ".join(
        f"{item.stamp} {item.xacts_per_s} xacts/s query={item.query_us / 1000:.0f}ms"
        for item in samples
    )


def _alert(window: Sequence[StallSample]) -> str:
    return (
        f"DB write path stalled for >= {_WINDOW} consecutive minutes "
        f"(xacts/s < {_MIN_XACTS} and average query > {_MAX_QUERY_US // 1000} ms):\n"
        f"{_describe(window)}\n"
        "This is the root-cause signal; agent-host crashes are its consequence. "
        "Check host memory and the largest cgroup units "
        "(~/.meiva/logs/host-memory.jsonl)."
    )


def plan_poll(state: WatchState, fresh: Sequence[StallSample], now: float) -> PollPlan:
    seen = state.seen
    window = list(state.window)
    last_alert = state.last_alert
    alerted = state.alerted
    log_lines: list[str] = []
    alert: str | None = None
    advanced = False
    for sample in fresh:
        if seen is not None and sample.stamp <= seen:
            continue
        advanced = True
        seen = sample.stamp
        window = [*window, sample][-_MEMORY:]
        if _stalled(sample):
            log_lines.append(f"stalled: {_describe([sample])}")
        if _confirmed(window):
            if now - last_alert >= _COOLDOWN_SECONDS:
                last_alert = now
                alerted = True
                alert = _alert(window)
        elif alerted:
            alerted = False
            log_lines.append(f"recovered: {_describe([sample])}")
    return PollPlan(
        WatchState(seen, tuple(window), last_alert, alerted), tuple(log_lines), alert, advanced
    )
