# meiva:link schedules/docs/self-evolution.meiva.okf.md

import os
import sys
from datetime import UTC, datetime
from functools import partial

from base.paths.home import resolve_meiva_home
from schedules.jobs.entry import cluster_timezone, open_schedule_entry
from schedules.jobs.loop import CronSchedule, Trigger, run_loop
from schedules.jobs.self_evolution_scan import DailyScan, run_daily_scan
from schedules.plans.loop_plan import LoopTiming
from schedules.plans.scan_outcome import parse_daily_report_agent
from schedules.plans.schedule_identity import parse_schedule_id

CRON = "0 0 * * *"
_REPORT_AGENT_ENV = "MEIVA_SELF_EVOLUTION_DAILY_REPORT_AGENT"

# meiva:note
# The measured full day is about 113 minutes with two collect streams running
# (2026-09-25), so 180 minutes is roughly 1.6x contended time: the bound separates a slow
# scan from a wedged one, and the extra margin only delays wedge detection.
# meiva:link schedules/docs/self-evolution.meiva.okf.md
_SCAN_TIMEOUT_SECONDS = 10800


if __name__ == "__main__":
    home = resolve_meiva_home()
    with open_schedule_entry(globals().get("MEIVA_SCHEDULE_INPUTS"), home=home) as inputs:
        daily_scan = home / "skills" / "meiva-self-evolution" / "reference" / "daily_scan.py"
        scan = DailyScan(
            command=(sys.executable, str(daily_scan), "--days", "1"),
            script=daily_scan,
            timeout=_SCAN_TIMEOUT_SECONDS,
            report_agent=parse_daily_report_agent(os.environ.get(_REPORT_AGENT_ENV)),
        )
        run_loop(
            inputs.database(),
            CronSchedule(
                (Trigger(CRON),), timezone=cluster_timezone(), timing=LoopTiming(fire_ahead=60.0)
            ),
            partial(run_daily_scan, scan=scan),
            schedule_id=parse_schedule_id(os.environ.get("MEIVA_SCHEDULE_ID")),
            started_at=datetime.now(UTC),
        )
