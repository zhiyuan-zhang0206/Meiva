# meiva:note
# The weekly self-evolution trigger: count the agent events of the past week through the
# gateway's `/api/events` and wake the self-evolution agent when the volume clears its bar.
#
# The count carries `with_total=1`: since the 2026-08-18 contract change `meta.total` is opt-in,
# and without the flag the gateway returns no total, which crashed the trigger on `int(None)`
# while a silent 0 would have skipped the week's deep run. A reply without a total raises.
# The follow-up slot is armed by the main check, in this process only: a restart between the
# two forgets it, as it always has.
# meiva:link schedules/docs/self-evolution.meiva.okf.md

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import NoReturn

import httpx

from base.db import Database
from base.host.net.auth import delivered_token
from schedules.jobs.agent_directory import ensure_agent
from schedules.jobs.loop import CronSchedule, loop_once
from schedules.plans.weekly_trigger import (
    FOLLOW_UP_WINDOW_DAYS,
    WEEK_DAYS,
    WeeklyDecision,
    decide_follow_up,
    decide_main,
)

__all__ = [
    "AGENT_LABEL",
    "FOLLOW_UP_TRIGGER",
    "MAIN_TRIGGER",
    "EventsApi",
    "WeeklyState",
    "count_events",
    "handle_weekly_trigger",
    "read_events_api",
    "run_weekly",
    "weekly_once",
]

AGENT_LABEL = "self-evolution"
MAIN_TRIGGER = "main"
FOLLOW_UP_TRIGGER = "follow-up"


@dataclass(frozen=True)
class EventsApi:
    base_url: str
    token: str


# meiva:note
# The cluster secret is the delivered token of the environment, else the `MEIVA_CLUSTER_SECRET`
# line of the home's `.env`; the gateway URL is `MEIVA_GATEWAY_URL`, local by default. The entry
# reads both once and hands the result down.
def read_events_api(env: Mapping[str, str], home: Path) -> EventsApi:
    secret = delivered_token(env) or ""
    env_file = home / ".env"
    if not secret and env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("MEIVA_CLUSTER_SECRET="):
                secret = line.split("=", 1)[1].strip().strip("\"'")
    return EventsApi(env.get("MEIVA_GATEWAY_URL", "http://localhost:8000"), secret)


def count_events(api: EventsApi, since: datetime, until: datetime) -> int:
    headers = {"Authorization": f"Bearer {api.token}"} if api.token else {}
    params: dict[str, str | int] = {
        "from": since.isoformat(),
        "to": until.isoformat(),
        "limit": 1,
        "offset": 0,
        "with_total": 1,
    }
    resp = httpx.get(f"{api.base_url}/api/events", params=params, headers=headers, timeout=120.0)
    resp.raise_for_status()
    payload = resp.json()
    total = payload.get("meta", {}).get("total")
    if total is None:
        raise RuntimeError(
            f"/api/events returned no total despite with_total=1: {str(payload)[:300]}"
        )
    return int(total)


def handle_weekly_trigger(
    slot: datetime, trigger: str | None, *, api: EventsApi, prompt: str
) -> bool:
    if trigger == MAIN_TRIGGER:
        decision = decide_main(
            count_events(api, slot - timedelta(days=WEEK_DAYS), slot), slot, prompt
        )
    elif trigger == FOLLOW_UP_TRIGGER:
        since = slot - timedelta(days=FOLLOW_UP_WINDOW_DAYS)
        decision = decide_follow_up(count_events(api, since, slot), slot, prompt)
    else:
        raise ValueError(f"unknown self-evolution weekly trigger: {trigger}")
    _carry_out(decision, slot)
    return decision.follow_up_next


def _carry_out(decision: WeeklyDecision, now: datetime) -> None:
    for line in decision.log_lines:
        print(line)
    if decision.prompt is not None:
        dispatch = ensure_agent(AGENT_LABEL, decision.prompt)
        print(f"[{now.isoformat()}] self-evolution agent #{dispatch.agent_id} triggered")


# meiva:note
# What the weekly loop remembers between iterations: the search floor and whether the follow-up
# slot is armed. The main trigger arms it, in this process only; a restart between the two
# forgets it, as it always has.
@dataclass(frozen=True)
class WeeklyState:
    floor: datetime
    follow_up_armed: bool


# meiva:note
# One iteration: only the main trigger is searched until a fire arms the follow-up, and a fire
# of either trigger returns the next value of that flag. Returns the new state and the sleep.
def weekly_once(
    db: Database,
    schedule: CronSchedule,
    fire: Callable[[datetime, str | None], bool],
    *,
    state: WeeklyState,
    now: datetime,
    schedule_id: int,
) -> tuple[WeeklyState, float]:
    active = replace(
        schedule,
        triggers=tuple(
            item
            for item in schedule.triggers
            if state.follow_up_armed or item.payload == MAIN_TRIGGER
        ),
    )
    tick = loop_once(db, active, fire, floor=state.floor, now=now, schedule_id=schedule_id)
    armed = state.follow_up_armed if tick.fired is None else tick.fired.result
    return WeeklyState(tick.floor, armed), tick.sleep


def run_weekly(
    db: Database,
    schedule: CronSchedule,
    fire: Callable[[datetime, str | None], bool],
    *,
    schedule_id: int,
    started_at: datetime,
    clock: Callable[[], datetime],
) -> NoReturn:
    state = WeeklyState(floor=started_at, follow_up_armed=False)
    while True:
        state, sleep = weekly_once(
            db,
            schedule,
            fire,
            state=state,
            now=clock(),
            schedule_id=schedule_id,
        )
        time.sleep(sleep)
