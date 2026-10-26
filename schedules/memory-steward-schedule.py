# meiva:link schedules/README.md

import os
from datetime import UTC, datetime
from functools import partial

from base.paths.home import resolve_meiva_home
from schedules.jobs.catchup import cluster_timezone
from schedules.jobs.entry import open_schedule_entry
from schedules.jobs.loop import CronSchedule, Trigger, run_loop
from schedules.jobs.memory_maintenance import wake_arbiter
from schedules.plans.loop_plan import LoopTiming
from schedules.plans.schedule_identity import parse_schedule_id

MEMORY_ARBITRATOR_LABEL = "memory-arbiter"

# meiva:note
# The 12:00 merge trigger was removed on 2026-08-01: merge/close/revert is the merge
# captain's job, not the arbiter's.
TRIGGERS = (
    Trigger("0 4 * * *", "Daily consolidation (commit + push + PR merge)"),
    Trigger("0 7 * * *", "Daily health check — OKF, size, dedup, stale"),
)


if __name__ == "__main__":
    home = resolve_meiva_home()
    with open_schedule_entry(globals().get("MEIVA_SCHEDULE_INPUTS"), home=home) as inputs:
        run_loop(
            inputs.database(),
            CronSchedule(TRIGGERS, timezone=cluster_timezone(), timing=LoopTiming(rest=60.0)),
            partial(wake_arbiter, label=MEMORY_ARBITRATOR_LABEL),
            schedule_id=parse_schedule_id(os.environ.get("MEIVA_SCHEDULE_ID")),
            started_at=datetime.now(UTC),
        )
