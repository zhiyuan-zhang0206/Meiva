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
from schedules.jobs.agent_directory import ensure_agent
from schedules.jobs.loop import CronSchedule, loop_once, run_rounds
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
# The gateway credential comes from the same decision every other caller uses (`gateway_bearer`),
# fed by the same parts: the machine token this process was launched with, else the home's own
# secret, read from its `.env` on every run so a rotation reaches this weekly job without a restart.
# What it must not do is send a request with no credential at all: on a trusted-network deployment
# that is served as the cluster administrator, on any other it is refused, and the same job changing
# identity with a deployment flag is the inconsistency this convergence removes.
# meiva:link docs/decisions/data/security/2026-10-03-cluster-secret-contraction.md
def read_events_api(env: Mapping[str, str], home: Path) -> EventsApi:
    from base.cluster.machine import GatewayAccess, GatewayApiTokenMissing, gateway_bearer
    from base.host.env import dotenv_file
    from base.host.env.bootstrap import launch_facts

    env_file = dotenv_file.read_env_aliases(dotenv_file.env_file_path(home))
    facts = launch_facts(env, env_file=env_file)
    secret = env.get("MEIVA_CLUSTER_SECRET") or env_file.get("MEIVA_CLUSTER_SECRET") or ""
    try:
        token = gateway_bearer(
            GatewayAccess(
                gateway_url="",
                cluster_secret=secret,
                remote_plane=False,
                machine_token=facts.machine_token,
                launcher_profile=facts.launcher_profile,
                gateway_home=facts.gateway_home,
            )
        )
    except GatewayApiTokenMissing as exc:
        raise RuntimeError(
            f"the self-evolution trigger has no gateway credential on home {home}: {exc}"
        ) from exc
    return EventsApi(env.get("MEIVA_GATEWAY_URL", "http://localhost:8000"), token)


def count_events(api: EventsApi, since: datetime, until: datetime) -> int:
    # meiva:note
    # A boundary check, not a second policy: a caller that reached here without a credential is a
    # bug, and an anonymous request would be answered as the administrator on a trusted-network
    # deployment. `read_events_api` is the only producer of an `EventsApi` and never returns one
    # without a token.
    if not api.token:
        raise RuntimeError("refusing to read events with no gateway credential")
    headers = {"Authorization": f"Bearer {api.token}"}
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


# meiva:note
# The weekly loop is `run_rounds` with one round of its own: it carries `WeeklyState` between rounds
# and reads the clock once per round, so a round that raises leaves the floor where it was and the
# follow-up slot as it was armed. The beat, the backoff and the schedule-row write are the driver's;
# a second copy of them here is how the two weekly and daily schedules would end up failing
# differently. `home` and `sleep` are optional for the same reason they are on `run_loop`: the
# stored copy of this script in the schedules table predates them.
def run_weekly(
    db: Database,
    schedule: CronSchedule,
    fire: Callable[[datetime, str | None], bool],
    *,
    schedule_id: int,
    started_at: datetime,
    clock: Callable[[], datetime],
    home: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> NoReturn:
    def advance(now: datetime, state: WeeklyState) -> tuple[WeeklyState, float]:
        return weekly_once(
            db,
            schedule,
            fire,
            state=state,
            now=now,
            schedule_id=schedule_id,
        )

    run_rounds(
        db,
        schedule,
        advance,
        state=WeeklyState(floor=started_at, follow_up_armed=False),
        schedule_id=schedule_id,
        clock=clock,
        home=home,
        sleep=sleep,
    )
