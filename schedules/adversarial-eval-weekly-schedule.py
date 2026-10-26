# meiva:link schedules/docs/adversarial-eval.meiva.okf.md

import os
from datetime import UTC, datetime
from functools import partial

from base.paths.home import resolve_meiva_home
from schedules.jobs.adversarial_batch import BatchConfig, fire_weekly_batch
from schedules.jobs.catchup import cluster_timezone
from schedules.jobs.entry import open_schedule_entry
from schedules.jobs.loop import CronSchedule, Trigger, run_loop
from schedules.plans.loop_plan import LoopTiming
from schedules.plans.schedule_identity import parse_schedule_id

CRON = "0 4 * * 3"

# meiva:note
# The loop reads the clock at least every 60 seconds, the poll interval this schedule has always
# had, so a changed clock delays the weekly slot by no more than a minute. The batch runs
# inside the claim, so nothing rests after it.
_TIMING = LoopTiming(max_sleep=60.0, rest=0.0)


if __name__ == "__main__":
    home = resolve_meiva_home()
    with open_schedule_entry(globals().get("MEIVA_SCHEDULE_INPUTS"), home=home) as inputs:
        config = BatchConfig(root=home / "adversarial_eval")
        run_loop(
            inputs.database(),
            CronSchedule((Trigger(CRON),), timezone=cluster_timezone(), timing=_TIMING),
            partial(fire_weekly_batch, config=config),
            schedule_id=parse_schedule_id(os.environ.get("MEIVA_SCHEDULE_ID")),
            started_at=datetime.now(UTC),
        )
