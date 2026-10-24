# meiva:link schedules/docs/self-evolution.meiva.okf.md

import os
from datetime import UTC, datetime
from functools import partial

from base.paths.home import resolve_meiva_home
from schedules.jobs.catchup import cluster_timezone
from schedules.jobs.entry import open_schedule_entry
from schedules.jobs.loop import CronSchedule, Trigger
from schedules.jobs.self_evolution_weekly import (
    FOLLOW_UP_TRIGGER,
    MAIN_TRIGGER,
    handle_weekly_trigger,
    read_events_api,
    run_weekly,
)
from schedules.plans.loop_plan import LoopTiming
from schedules.plans.schedule_identity import parse_schedule_id

PROMPT = "Read and run $MEIVA_HOME/skills/meiva-self-evolution/SKILL.md for this week."

# meiva:note
# The main check is Tuesday 00:00 and the mid-week follow-up Friday 00:00 cluster time; the
# trigger name travels in the slot payload.
MAIN_CRON = "0 0 * * 2"
FOLLOW_UP_CRON = "0 0 * * 5"


if __name__ == "__main__":
    with open_schedule_entry(globals().get("MEIVA_SCHEDULE_INPUTS")) as inputs:
        run_weekly(
            inputs.database(),
            CronSchedule(
                (Trigger(MAIN_CRON, MAIN_TRIGGER), Trigger(FOLLOW_UP_CRON, FOLLOW_UP_TRIGGER)),
                timezone=cluster_timezone(),
                timing=LoopTiming(fire_ahead=90.0),
            ),
            partial(
                handle_weekly_trigger,
                api=read_events_api(os.environ, resolve_meiva_home()),
                prompt=PROMPT,
            ),
            schedule_id=parse_schedule_id(os.environ.get("MEIVA_SCHEDULE_ID")),
            started_at=datetime.now(UTC),
        )
