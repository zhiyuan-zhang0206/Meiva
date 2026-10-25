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
from schedules.plans.stall_rule import WatchState, parse_stats, plan_poll

__all__ = ["StallWatch", "initial_state", "poll_once", "run_watch"]

_TAIL_LINES = 40


# meiva:note
# `log` is PgBouncer's log, `state` the watcher's own small file, `zone` the cluster zone its
# output is stamped in (the zone of the dashboards the operator reads), and `report_agents`
# the operators an alert goes to.
@dataclass(frozen=True)
class StallWatch:
    log: Path
    state: Path
    zone: ZoneInfo
    report_agents: tuple[int, ...]
    poll_seconds: float = 60.0


def _stamp(now: float, zone: ZoneInfo) -> str:
    return f"[{datetime.fromtimestamp(now, zone):%Y-%m-%d %H:%M:%S %Z}]"


def _read_last_alert(path: Path) -> float:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0.0
    value = payload.get("last_alert")
    return float(value) if isinstance(value, (int, float)) else 0.0


def _write_state(path: Path, *, last_alert: float, last_sample: str | None, now: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "last_alert": last_alert,
                "last_sample": last_sample,
                "updated": datetime.fromtimestamp(now, UTC).isoformat(timespec="seconds"),
            }
        ),
        encoding="utf-8",
    )


def initial_state(watch: StallWatch) -> WatchState:
    return WatchState(seen=None, window=(), last_alert=_read_last_alert(watch.state), alerted=False)


def poll_once(watch: StallWatch, state: WatchState, now: float) -> WatchState:
    try:
        lines = watch.log.read_text(encoding="utf-8", errors="replace").splitlines()[-_TAIL_LINES:]
    except OSError:
        lines = []
    plan = plan_poll(state, parse_stats(lines), now)
    for line in plan.log_lines:
        print(f"{_stamp(now, watch.zone)} {line}", flush=True)
    if plan.advanced:
        _write_state(
            watch.state, last_alert=plan.state.last_alert, last_sample=plan.state.seen, now=now
        )
    if plan.alert is not None:
        for agent in watch.report_agents:
            try:
                meiva.agents.send_message(agent, plan.alert)
            except Exception as exc:
                print(f"db-write-stall-watch could not notify {agent}: {exc}", flush=True)
    return plan.state


def run_watch(watch: StallWatch, clock: Callable[[], float]) -> NoReturn:
    state = initial_state(watch)
    while True:
        now = clock()
        try:
            state = poll_once(watch, state, now)
        except Exception as exc:
            print(
                f"{_stamp(now, watch.zone)} probe failed: {type(exc).__name__}: {exc}", flush=True
            )
        time.sleep(watch.poll_seconds)
