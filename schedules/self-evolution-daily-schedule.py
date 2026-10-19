# meiva:link schedules/docs/self-evolution.meiva.okf.md

from uuid import uuid4

import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta

import meiva
from meiva.agents import AgentStatus as S
from schedules.agent_status_guard import ensure_agent_status_members
from schedules.catchup import cluster_timezone, fire_slot_once
from base.daemon.schedules.watcher import next_fire
from base.daemon.schedules.inputs import ScheduleInputs
from schedules.entry import fresh_schedule_entry, schedule_entry


# meiva:note
# `daily_scan.py` ships with the meiva-self-evolution skill, and this reads the skill's
# load-directory copy: converge-managed but bootstrap-only (R5), refreshed to the landed
# revision by the rollout's update legs, with `meiva skill update` as the manual.
# meiva:link schedules/docs/self-evolution.meiva.okf.md
def _daily_scan_path() -> str:
    return os.path.join(
        os.environ.get("MEIVA_HOME", os.path.expanduser("~/.meiva")),
        "skills",
        "meiva-self-evolution",
        "reference",
        "daily_scan.py",
    )


CRON = "0 0 * * *"


def _report_agent() -> str:
    return os.environ.get("MEIVA_SELF_EVOLUTION_DAILY_REPORT_AGENT", "228")


# meiva:note
# The measured full day is about 113 minutes with two collect streams running
# (2026-09-25), so 180 minutes is roughly 1.6x contended time: the bound separates a slow
# scan from a wedged one, and the extra margin only delays wedge detection.
# meiva:link schedules/docs/self-evolution.meiva.okf.md
_SCAN_TIMEOUT_SECONDS = 10800


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


def run_scan() -> None:
    daily = _daily_scan_path()
    if not os.path.isfile(daily):
        # meiva:note
        # A missing script must not masquerade as an ALERT: python exits 2 when it cannot
        # open the file, which the rc==2 branch would read as "bad runs found".
        msg = (
            f"daily_scan.py missing at {daily} — run `meiva skill update meiva-self-evolution` "
            f"(or `meiva skill update` for all repo-native skills) to refresh the load-dir copy"
        )
        print(f"[{datetime.now(UTC).isoformat()}] {msg}")
        ensure_agent("self-evolution", f"Daily scan cannot run: {msg}")
        return
    try:
        r = subprocess.run(
            [sys.executable, daily, "--days", "1"],
            timeout=_SCAN_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
            check=False,
        )
        tail = (r.stdout or "")[-2000:]
        err = (r.stderr or "")[-500:]
        print(f"[{datetime.now(UTC).isoformat()}] scan rc={r.returncode}")
        print(tail)
        if r.returncode == 2:
            ensure_agent(
                "self-evolution",
                f"Daily scan ALERT ({datetime.now(UTC).isoformat()}):\n{tail}\n{err}\nReview the daily report and act.",
            )
        elif r.returncode != 0:
            print(f"scan failed rc={r.returncode}: {err}")
            ensure_agent(
                "self-evolution",
                f"Daily scan failed rc={r.returncode}:\n{err}\nCheck the schedule log and daily_scan.py.",
            )
    except subprocess.TimeoutExpired:
        print(f"scan timed out after {_SCAN_TIMEOUT_SECONDS}s")
        ensure_agent(
            "self-evolution",
            f"Daily scan timed out after {_SCAN_TIMEOUT_SECONDS}s — check whether daily_scan.py is stuck.",
        )
        return
    report_agent = _report_agent()
    if report_agent:
        try:
            meiva.agents.send_message(
                int(report_agent),
                f"[self-evolution daily {datetime.now(UTC).strftime('%m-%d')}]\n{tail}",
            )
        except Exception as e:
            print(f"daily report to {report_agent} failed: {e}")


def _fire_scan(_slot: datetime, _trigger: None) -> None:
    run_scan()


def main(*, inputs: ScheduleInputs) -> None:
    db = inputs.database()
    started_at = datetime.now(UTC)
    while True:
        # meiva:note
        # `after=now-2min` is the trigger tolerance: croniter's get_next is strictly
        # greater than its base, so a `now` a fraction of a second past the hour would jump
        # to the next day (deterministic miss, observed 2026-08-06). Window: [-120s, +60s].
        now = datetime.now(UTC)
        nxt = next_fire(
            CRON, after=max(started_at, now - timedelta(minutes=2)), timezone=cluster_timezone()
        )
        wait = (nxt - now).total_seconds()
        if wait > 60:
            time.sleep(min(wait, 3600))
            continue
        fire_slot_once(db, nxt, None, fire=_fire_scan)
        time.sleep(120)


if __name__ == "__main__":
    ensure_agent_status_members(
        S,
        {"TERMINATED"},
        schedule_name="self-evolution-daily",
    )
    entry_inputs = globals().get("MEIVA_SCHEDULE_INPUTS")
    if entry_inputs is None:
        from base.log import prepare_process_logging

        prepare_process_logging()
    with fresh_schedule_entry() if entry_inputs is None else schedule_entry(entry_inputs) as inputs:
        main(inputs=inputs)
