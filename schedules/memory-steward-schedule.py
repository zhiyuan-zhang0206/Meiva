# meiva:link schedules/README.md

import os
from datetime import UTC, datetime
from functools import partial

from base.paths.home import resolve_meiva_home
from schedules.jobs.entry import cluster_timezone, open_schedule_entry
from schedules.jobs.loop import CronSchedule, Trigger, run_loop
from schedules.jobs.memory_maintenance import wake_arbiter
from schedules.plans.loop_plan import LoopTiming
from schedules.plans.schedule_identity import parse_schedule_id

MEMORY_ARBITRATOR_LABEL = "memory-arbiter"

# meiva:note
# The 12:00 merge trigger was removed on 2026-08-01: merge/close/revert is the merge
# captain's job, not the arbiter's.
TRIGGERS = (
    Trigger(
        "0 4 * * *",
        "Daily consolidation (rebuild indexes, commit, push, PR merge) — first run, in this"
        " order: `~/.meiva/source/.venv/bin/python ~/.meiva/source/meiva_builtins/plugins/"
        "meiva_memory/skills/scripts/rebuild_memory_index.py`, then the same interpreter on"
        " `~/.meiva/source/meiva_builtins/plugins/meiva_memory/skills/scripts/gen_indexes.py`"
        " (both take no arguments and resolve the Meiva home themselves; bare script names are"
        " not on PATH and `python3` cannot import `base`),"
        " then merge only PRs whose `validate` check is green, whose author is not the merger,"
        " and write the collector snapshot (each face's HEAD + dirty count) into the PR body"
        " first",
    ),
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
            home=inputs.home,
            started_at=datetime.now(UTC),
            clock=lambda: datetime.now(UTC),
        )
