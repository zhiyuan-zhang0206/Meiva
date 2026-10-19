# meiva:link schedules/docs/self-evolution.meiva.okf.md

from uuid import uuid4

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import meiva
from meiva.agents import AgentStatus as S
from schedules.agent_status_guard import ensure_agent_status_members
from schedules.catchup import cluster_timezone, fire_slot_once
from base.daemon.schedules.watcher import next_fire
from base.daemon.schedules.inputs import ScheduleInputs
from schedules.entry import fresh_schedule_entry, schedule_entry


LABEL = "self-evolution"
PROMPT = "Read and run $MEIVA_HOME/skills/meiva-self-evolution/SKILL.md for this week."

# meiva:note
# Despite the MONDAY_*/THURSDAY_* names, the main check is Tuesday 00:00 and the mid-week
# follow-up Friday 00:00 cluster time; the trigger values travel in the slot payload.
MONDAY_CRON = "0 0 * * 2"
THURSDAY_CRON = "0 0 * * 5"
MONDAY_TRIGGER = "monday"
THURSDAY_TRIGGER = "thursday"

# meiva:note
# A weekly floor of about 4K events a day: below it the week is too quiet for meaningful
# analysis, so no pass runs at all.
MIN_WEEKLY_EVENTS = 30000

# meiva:note
# Above this many events in the week, the main trigger also enables the mid-week follow-up.
HIGH_WEEKLY_EVENTS = 200000


def count_events(since: datetime) -> int:
    import os

    import httpx

    from base.paths import meiva_home

    from base.host.net.auth import delivered_token

    env_file = Path(meiva_home()) / ".env"
    secret = delivered_token() or ""
    if not secret and env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("MEIVA_CLUSTER_SECRET="):
                secret = line.split("=", 1)[1].strip().strip("\"'")
    headers = {"Authorization": f"Bearer {secret}"} if secret else {}
    base = os.environ.get("MEIVA_GATEWAY_URL", "http://localhost:8000")
    params = {
        "from": since.isoformat(),
        "to": datetime.now(UTC).isoformat(),
        "limit": 1,
        "offset": 0,
        # meiva:note
        # `meta.total` is opt-in since the 2026-08-18 contract change: without this flag the
        # gateway returns no total, which crashes the trigger on `int(None)`, while a silent
        # 0 would skip the week's deep run.
        "with_total": 1,
    }
    resp = httpx.get(f"{base}/api/events", params=params, headers=headers, timeout=120.0)
    resp.raise_for_status()
    payload = resp.json()
    total = payload.get("meta", {}).get("total")
    if total is None:
        raise RuntimeError(
            f"/api/events returned no total despite with_total=1: {str(payload)[:300]}"
        )
    return int(total)


def ensure_agent(label: str, prompt: str) -> int:
    before_id = None
    while True:
        page = meiva.agents.list_agents(scope="all", query=label, before_id=before_id)
        for agent in page.agents:
            if agent.label != label:
                continue
            if agent.status == S.TERMINATED:
                meiva.agents.resurrect(agent.agent_id, prompt)
            else:
                meiva.agents.send_message(agent.agent_id, prompt)
            return agent.agent_id
        if page.next_cursor is None:
            break
        before_id = page.next_cursor
    return meiva.agents.spawn(prompt=prompt, label=label, idempotency_key=str(uuid4()))


def fire(prompt: str) -> None:
    agent_id = ensure_agent(LABEL, prompt)
    print(f"[{datetime.now(UTC).isoformat()}] self-evolution agent #{agent_id} triggered")


def should_fire_monday() -> bool:
    week_ago = datetime.now(UTC) - timedelta(days=7)
    total = count_events(week_ago)
    print(
        f"[{datetime.now(UTC).isoformat()}] "
        f"self-evolution check: {total} events in past 7 days "
        f"(min={MIN_WEEKLY_EVENTS}, high={HIGH_WEEKLY_EVENTS})"
    )
    if total < MIN_WEEKLY_EVENTS:
        print(
            f"[{datetime.now(UTC).isoformat()}] "
            f"self-evolution skipped: {total} < {MIN_WEEKLY_EVENTS} minimum"
        )
        return False
    return True


# meiva:note
# The follow-up bar is half the weekly high threshold, counted from the main trigger rather
# than from the whole week.
def should_fire_thursday() -> bool:
    monday = datetime.now(UTC) - timedelta(days=4)
    count = count_events(monday)
    bar = HIGH_WEEKLY_EVENTS // 2
    print(
        f"[{datetime.now(UTC).isoformat()}] "
        f"self-evolution Thursday check: {count} events since Monday "
        f"(bar={bar})"
    )
    return count >= bar


def _main_loop(*, inputs: ScheduleInputs) -> None:
    db = inputs.database()
    thursday_enabled = False

    def fire_weekly_trigger(_slot: datetime, trigger: str) -> None:
        nonlocal thursday_enabled
        if trigger == MONDAY_TRIGGER:
            if should_fire_monday():
                fire(PROMPT)
                week_ago = datetime.now(UTC) - timedelta(days=7)
                total = count_events(week_ago)
                thursday_enabled = total >= HIGH_WEEKLY_EVENTS
                if thursday_enabled:
                    print(
                        f"[{datetime.now(UTC).isoformat()}] self-evolution: "
                        f"Thursday follow-up enabled ({total} >= {HIGH_WEEKLY_EVENTS})"
                    )
            else:
                thursday_enabled = False
            return
        if trigger == THURSDAY_TRIGGER:
            if should_fire_thursday():
                fire(f"{PROMPT} (mid-week follow-up)")
            thursday_enabled = False
            return
        raise ValueError(f"unknown self-evolution weekly trigger: {trigger}")

    started_at = datetime.now(UTC)
    while True:
        now = datetime.now(UTC)

        # meiva:note
        # `after=now-2min` is the trigger tolerance: croniter's get_next is strictly greater
        # than its base, so a `now` a fraction of a second past the hour would jump to the
        # next day (deterministic miss, observed 2026-08-06). Window: [-120s, +90s].
        nxt_monday = next_fire(
            MONDAY_CRON,
            after=max(started_at, now - timedelta(minutes=2)),
            timezone=cluster_timezone(),
        )
        wait_monday = (nxt_monday - now).total_seconds()

        nxt_thursday = next_fire(
            THURSDAY_CRON,
            after=max(started_at, now - timedelta(minutes=2)),
            timezone=cluster_timezone(),
        )
        wait_thursday = (nxt_thursday - now).total_seconds() if thursday_enabled else float("inf")

        wait = min(wait_monday, wait_thursday)

        if wait > 90:
            time.sleep(min(wait, 3600))
            continue

        if wait_monday <= 90:
            fire_slot_once(
                db,
                nxt_monday,
                MONDAY_TRIGGER,
                fire=fire_weekly_trigger,
            )
            time.sleep(120)
            continue

        if wait_thursday <= 90:
            fire_slot_once(
                db,
                nxt_thursday,
                THURSDAY_TRIGGER,
                fire=fire_weekly_trigger,
            )
            time.sleep(120)
            continue


if __name__ == "__main__":
    ensure_agent_status_members(
        S,
        {"TERMINATED"},
        schedule_name="self-evolution-weekly",
    )
    entry_inputs = globals().get("MEIVA_SCHEDULE_INPUTS")
    if entry_inputs is None:
        from base.log import prepare_process_logging

        prepare_process_logging()
    with fresh_schedule_entry() if entry_inputs is None else schedule_entry(entry_inputs) as inputs:
        _main_loop(inputs=inputs)
