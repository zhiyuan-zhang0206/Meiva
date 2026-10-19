# meiva:link schedules/README.md

from uuid import uuid4

import time
from datetime import UTC, datetime
import meiva
from meiva.agents import AgentStatus as S
from schedules.agent_status_guard import ensure_agent_status_members
from schedules.catchup import cluster_timezone, fire_slot_once
from base.daemon.schedules.watcher import next_fire
from base.daemon.schedules.inputs import ScheduleInputs
from schedules.entry import fresh_schedule_entry, schedule_entry


MEMORY_ARBITRATOR_LABEL = "memory-arbiter"

# meiva:note
# The 12:00 merge trigger was removed on 2026-08-01: merge/close/revert is the merge
# captain's job, not the arbiter's.
TRIGGERS = [
    ("0 4 * * *", "Daily consolidation (commit + push + PR merge)"),
    ("0 7 * * *", "Daily health check — OKF, size, dedup, stale"),
]


def ensure_agent(label, prompt):
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


def fire_memory_maintenance(_slot: datetime, message: str) -> None:
    print(f"[schedule] Firing: {message}")
    try:
        full_prompt = f"memory-arbiter: {message}"
        ensure_agent(MEMORY_ARBITRATOR_LABEL, full_prompt)
        print(f"[schedule] Agent notified: {message}")
    except Exception as exc:
        print(f"[schedule] Failed: {exc}")


def main(*, inputs: ScheduleInputs) -> None:
    db = inputs.database()
    while True:
        now = datetime.now(UTC)
        next_times = []
        for cron_expr, message in TRIGGERS:
            nxt = next_fire(cron_expr, after=now, timezone=cluster_timezone())
            next_times.append((nxt, cron_expr, message))

        next_fire_time, cron_expr, message = min(next_times, key=lambda x: x[0])

        wait = (next_fire_time - datetime.now(UTC)).total_seconds()
        if wait > 0:
            print(f"[schedule] Next: {next_fire_time} ({cron_expr}) — sleeping {wait:.0f}s")
            time.sleep(wait)

        fire_slot_once(db, next_fire_time, message, fire=fire_memory_maintenance)

        time.sleep(60)


if __name__ == "__main__":
    ensure_agent_status_members(
        S,
        {"TERMINATED"},
        schedule_name="memory-arbiter",
    )
    entry_inputs = globals().get("MEIVA_SCHEDULE_INPUTS")
    if entry_inputs is None:
        from base.log import prepare_process_logging

        prepare_process_logging()
    with fresh_schedule_entry() if entry_inputs is None else schedule_entry(entry_inputs) as inputs:
        main(inputs=inputs)
