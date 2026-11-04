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
        "Daily consolidation (rebuild indexes, commit, push, PR merge) — first run, in this order"
        ": `W=$(mktemp -d)/pool; FH=$(dirname $W)/fh; git -C ~/.meiva/memory worktree add -q -B m"
        "em/rebuild-$(date +%m%d-%H%M) $W origin/main; mkdir -p $FH; ln -sfn $W $FH/memory; cd $W"
        "; MEIVA_HOME=$FH ~/.meiva/source/.venv/bin/python ~/.meiva/source/meiva_builtins/plugins"
        "/meiva_memory/skills/scripts/rebuild_memory_index.py; MEIVA_HOME=$FH ~/.meiva/source/.ve"
        "nv/bin/python ~/.meiva/source/meiva_builtins/plugins/meiva_memory/skills/scripts/gen_ind"
        "exes.py` (the two scripts resolve the pool from MEIVA_HOME and take no arguments; the fa"
        "ke home is what points them at the worktree), then commit + push that branch + open a PR"
        " and squash-merge it once `validate` is green — a pure generated artifact, so the author"
        " may merge it; **never commit on the shared checkout's main: the pool pre-commit refuses"
        " it**, then merge only review PRs whose `validate` check is green, whose author is not t"
        "he merger, and write the collector snapshot (each face's HEAD + dirty count) into the PR"
        " body first",
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
