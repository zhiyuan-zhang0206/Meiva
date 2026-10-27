# meiva:note
# The agent watch: every ten minutes, ask whether each agent with a watching duty is still taking
# its rounds, and wake it, tell the lead, or escalate along the rule in
# `schedules/plans/agent_watch_rule.py`.
#
# It exists because a duty that goes quiet looks exactly like a duty that has nothing to report:
# on 2026-10-23 seventeen of one monitor's watchers were killed with the runtime, nothing rebuilt
# them, and the machine was unmonitored for five hours while every dashboard stayed green. The
# round reads the signals a stopped duty leaves behind (no live session, no real turn, a schedule
# that stopped firing) rather than waiting for the duty to announce its own death.
#
# The watched list and its cadences live in the rule module, not here: provisioning rewrites a
# schedule's stored script from this template, so a duty baked into this file would be forced onto
# every site that runs it. Only the report agent is a site value, overridable through
# `MEIVA_AGENT_WATCH_REPORT_AGENT`.
# meiva:link schedules/README.md

import os
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from functools import partial
from zoneinfo import ZoneInfo

from base.paths.home import resolve_meiva_home
from schedules.jobs.agent_watch import run_round
from schedules.jobs.catchup import cluster_timezone
from schedules.jobs.entry import open_schedule_entry
from schedules.jobs.loop import CronSchedule, Trigger, run_loop
from schedules.plans.schedule_identity import parse_schedule_id

CRON = "*/10 * * * *"
_PROCESS_NAME = "agent-watch"
_REPORT_AGENT = 405
_ENV_REPORT_AGENT = "MEIVA_AGENT_WATCH_REPORT_AGENT"
_STATE_FILE = "agent-watch.json"


# meiva:note
# The lead this watch reports to. The default is the cluster's P0 lead; a site that reports
# elsewhere sets the variable, and a value that is not a positive id stops the round naming the
# variable instead of reporting into nothing.
def report_agent_from_environ(environ: Mapping[str, str]) -> int:
    raw = environ.get(_ENV_REPORT_AGENT, "").strip()
    if not raw:
        return _REPORT_AGENT
    try:
        agent_id = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{_ENV_REPORT_AGENT} must be a numeric agent id") from exc
    if agent_id <= 0:
        raise RuntimeError(f"{_ENV_REPORT_AGENT} must be a positive agent id")
    return agent_id


def watch_once(
    slot: datetime, _payload: str | None, *, inputs, report_agent: int, state_path
) -> None:
    line = run_round(
        inputs=inputs,
        report_agent=report_agent,
        state_path=state_path,
        now=datetime.now(UTC),
        process=_PROCESS_NAME,
    )
    zone = ZoneInfo(cluster_timezone())
    print(f"[{slot.astimezone(zone):%Y-%m-%d %H:%M %Z}] {line}", flush=True)


# meiva:note
# `--once` runs a single round and exits, which is how an operator checks the watch by hand before
# a wave provisions it; it claims no slot, so the cron ladder is untouched.
if __name__ == "__main__":
    home = resolve_meiva_home()
    with open_schedule_entry(globals().get("MEIVA_SCHEDULE_INPUTS"), home=home) as inputs:
        watch = partial(
            watch_once,
            inputs=inputs,
            report_agent=report_agent_from_environ(os.environ),
            state_path=home / "run" / _STATE_FILE,
        )
        if "--once" in sys.argv:
            watch(datetime.now(UTC), None)
            raise SystemExit(0)
        run_loop(
            inputs.database(),
            CronSchedule((Trigger(CRON),), timezone=cluster_timezone()),
            watch,
            schedule_id=parse_schedule_id(os.environ.get("MEIVA_SCHEDULE_ID")),
            started_at=datetime.now(UTC),
        )
