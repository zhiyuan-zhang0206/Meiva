# meiva:note
# Ship the local OTel trace mirror to the local Tempo viewer every five minutes.
#
# Incremental (a per-file watermark) so each run sends only new lines, and idempotent by span id,
# so a window that was missed is caught up on the next run. Best-effort on purpose: a failure is
# printed to the schedule log and retried on the next slot -- a viewer that is down for an hour
# must not take the schedule down with it and hide the run after that.
#
# This file is the repository template for the `trace-ship-tempo` built-in, not a second copy of
# the schedule: provisioning writes its text into the schedules table and the runner materializes
# that copy into the schedule's own directory, so an edit here reaches the cluster only through
# `meiva schedules provision` (2026-10-24, #5621: migrated to the post-`b191b5ec3` entry API).
# meiva:link schedules/README.md

from __future__ import annotations

import os
from datetime import UTC, datetime

import meiva
from base.paths.home import resolve_meiva_home
from schedules.jobs.catchup import cluster_timezone
from schedules.jobs.entry import open_schedule_entry
from schedules.jobs.loop import CronSchedule, Trigger, run_loop
from schedules.plans.loop_plan import LoopTiming
from schedules.plans.schedule_identity import parse_schedule_id

CRON = "*/5 * * * *"
SHIP_TIMEOUT_SECONDS = 600


# meiva:note
# One slot's worth of shipping. Every failure stays inside this function: the run loop reads an
# exception from a fire as the schedule's outcome, and a viewer that cannot be reached is not a
# reason to stop reporting the next slot.
def ship_traces(_slot: datetime, _payload: str | None) -> None:
    try:
        out = meiva.shell.run("meiva trace ship 2>&1 | tail -3", timeout=SHIP_TIMEOUT_SECONDS)
        print(out)
    except Exception as exc:
        print(f"trace ship failed: {exc}")


if __name__ == "__main__":
    home = resolve_meiva_home()
    with open_schedule_entry(globals().get("MEIVA_SCHEDULE_INPUTS"), home=home) as inputs:
        run_loop(
            inputs.database(),
            CronSchedule((Trigger(CRON),), timezone=cluster_timezone(), timing=LoopTiming()),
            ship_traces,
            schedule_id=parse_schedule_id(os.environ.get("MEIVA_SCHEDULE_ID")),
            started_at=datetime.now(UTC),
        )
