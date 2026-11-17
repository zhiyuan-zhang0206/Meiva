# meiva:note
# The operator sentinel for a stalled database write path.
#
# PgBouncer's per-minute stats line is the earliest host-local evidence that transactions
# stopped completing. Each poll reads the tail of the stats log, hands the lines it has not
# seen to `plan_poll`, prints what the plan says, saves its state, and sends the alert to the
# operators. It wakes the operator and the P0 lead only, never a user notification path.
# meiva:link schedules/README.md

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn
from zoneinfo import ZoneInfo

import meiva
from base.daemon.schedules.heartbeat import write_round_beat
from base.deploy.schedule_timing import SCHEDULE_FIRST_ROUND_BUDGET_S
from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_of
from schedules.plans.watch.stall_rule import (
    WATCH_MEMORY,
    StallSample,
    WatchState,
    parse_stats,
    plan_poll,
)

__all__ = ["StallWatch", "initial_state", "poll_once", "run_watch", "run_watch_once"]

_TAIL_LINES = 40
# meiva:note
# History retention: one line per PgBouncer minute is 1,440 a day at roughly 90 bytes each. Two
# weeks is the window the other host-history files keep, and 20,000 lines is about 2 MiB — past
# that the file is trimmed to its newest 15,000 lines, because an append-only monitoring aid with
# no ceiling becomes a disk alert of its own.
# meiva:link schedules/README.md
_HISTORY_MAX_LINES = 20_000
_HISTORY_KEEP_LINES = 15_000


# meiva:note
# `log` is PgBouncer's log, `state` the watcher's own small file, `zone` the cluster zone its
# output is stamped in (the zone of the dashboards the operator reads), and `report_agents`
# the operators an alert goes to.
@dataclass(frozen=True)
class StallWatch:
    log: Path
    state: Path
    history: Path
    zone: ZoneInfo
    report_agents: tuple[int, ...]
    poll_seconds: float = 60.0
    # meiva:note
    # Where the round beat goes. Both stay None for a stored script written before these parameters
    # existed, and then this loop reports nothing about itself -- unchanged behaviour, not a fault.
    home: Path | None = None
    schedule_id: int | None = None


def _stamp(now: float, zone: ZoneInfo) -> str:
    return f"[{datetime.fromtimestamp(now, zone):%Y-%m-%d %H:%M:%S %Z}]"


def _sample_from_payload(payload: object) -> StallSample | None:
    if not is_object_dict(payload):
        return None
    stamp = payload.get("stamp")
    xacts = payload.get("xacts_per_s")
    queries = payload.get("queries_per_s")
    query_us = payload.get("query_us")
    if (
        isinstance(stamp, str)
        and isinstance(xacts, int)
        and isinstance(queries, int)
        and isinstance(query_us, int)
    ):
        return StallSample(stamp, xacts, queries, query_us)
    return None


def _read_history_window(path: Path, *, through: str | None) -> tuple[StallSample, ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    samples: list[StallSample] = []
    for line in lines:
        try:
            sample = _sample_from_payload(object_of(json.loads, line))
        except ValueError:
            continue
        if sample is not None and (through is None or sample.stamp <= through):
            samples.append(sample)
    return tuple(samples[-WATCH_MEMORY:])


def _read_state(path: Path, *, history: Path) -> WatchState:
    try:
        payload = object_of(json.loads, path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return WatchState(seen=None, window=(), last_alert=0.0, alerted=False)
    if not is_object_dict(payload):
        return WatchState(seen=None, window=(), last_alert=0.0, alerted=False)
    raw_window = payload.get("window")
    window: list[StallSample] = []
    if is_object_list(raw_window):
        for raw in raw_window:
            sample = _sample_from_payload(raw)
            if sample is not None:
                window.append(sample)
    last_alert = payload.get("last_alert")
    seen = payload.get("last_sample")
    normalized_seen = seen if isinstance(seen, str) else None
    if not is_object_list(raw_window):
        window.extend(_read_history_window(history, through=normalized_seen))
    return WatchState(
        seen=normalized_seen,
        window=tuple(window[-WATCH_MEMORY:]),
        last_alert=float(last_alert) if isinstance(last_alert, (int, float)) else 0.0,
        alerted=payload.get("alerted") is True,
    )


def _write_state(path: Path, *, state: WatchState, now: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "last_alert": state.last_alert,
                "last_sample": state.seen,
                "alerted": state.alerted,
                "window": [
                    {
                        "stamp": sample.stamp,
                        "xacts_per_s": sample.xacts_per_s,
                        "queries_per_s": sample.queries_per_s,
                        "query_us": sample.query_us,
                    }
                    for sample in state.window
                ],
                "updated": datetime.fromtimestamp(now, UTC).isoformat(timespec="seconds"),
            }
        ),
        encoding="utf-8",
    )


# meiva:note
# `_append_history` records what the sentinel measured, so a past window can be read back: the
# state file answers "has it alerted", not "what did it see at 01:36". Raw values only — whether
# they breached stays the rule's business, and a reader can apply that rule without trusting this
# file's own verdict.
# meiva:link schedules/README.md
def _append_history(path: Path, samples: tuple[StallSample, ...], *, alerted: bool) -> None:
    if not samples:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            {
                "stamp": sample.stamp,
                "xacts_per_s": sample.xacts_per_s,
                "queries_per_s": sample.queries_per_s,
                "query_us": sample.query_us,
                "alert_sent": alerted,
                "source": "pgbouncer stats",
            }
        )
        for sample in samples
    ]
    existing: list[str] = []
    try:
        existing = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        existing = []
    existing.extend(lines)
    if len(existing) > _HISTORY_MAX_LINES:
        existing = existing[-_HISTORY_KEEP_LINES:]
    path.write_text("\n".join(existing) + "\n", encoding="utf-8")


def initial_state(watch: StallWatch) -> WatchState:
    return _read_state(watch.state, history=watch.history)


def poll_once(watch: StallWatch, state: WatchState, now: float) -> WatchState:
    try:
        lines = watch.log.read_text(encoding="utf-8", errors="replace").splitlines()[-_TAIL_LINES:]
    except OSError:
        lines = []
    plan = plan_poll(state, parse_stats(lines), now)
    for line in plan.log_lines:
        print(f"{_stamp(now, watch.zone)} {line}", flush=True)
    if plan.advanced:
        _write_state(watch.state, state=plan.state, now=now)
    _append_history(watch.history, plan.new_samples, alerted=plan.alert is not None)
    if plan.alert is not None:
        for agent in watch.report_agents:
            try:
                meiva.agents.send_message(agent, plan.alert)
            except Exception as exc:
                print(f"db-write-stall-watch could not notify {agent}: {exc}", flush=True)
    return plan.state


def run_watch_once(watch: StallWatch, clock: Callable[[], float]) -> None:
    began = clock()
    try:
        poll_once(watch, initial_state(watch), began)
    except Exception as exc:
        print(f"{_stamp(began, watch.zone)} probe failed: {type(exc).__name__}: {exc}", flush=True)


# meiva:note
# Every round promises the next one: the beat is written after the poll and before the sleep, and its
# budget is the slowest round this loop has actually taken. Without it a stalled poll and an idle
# watch are the same picture -- nothing printed, nothing recorded -- and this sentinel's whole job is
# to be believed while it says nothing.
def run_watch(watch: StallWatch, clock: Callable[[], float]) -> NoReturn:
    state = initial_state(watch)
    budget = SCHEDULE_FIRST_ROUND_BUDGET_S
    while True:
        began = clock()
        try:
            state = poll_once(watch, state, began)
        except Exception as exc:
            print(
                f"{_stamp(began, watch.zone)} probe failed: {type(exc).__name__}: {exc}", flush=True
            )
        ended = clock()
        budget = max(budget, ended - began)
        write_round_beat(
            watch.home,
            watch.schedule_id,
            now=datetime.fromtimestamp(ended, UTC),
            sleep_s=watch.poll_seconds,
            budget_s=budget,
        )
        time.sleep(watch.poll_seconds)
