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
        "Daily Meiva Dream consolidation (4-phase Orientation -> Gather Signal -> Consolidation -> "
        "Prune & Index, via isolated worktree + PR) — run in this order: "
        "1) Create an isolated worktree (**never commit on the shared checkout's main: the pool "
        "pre-commit refuses it**): `W=$(mktemp -d)/pool; FH=$(dirname $W)/fh; git -C ~/.meiva/memo"
        "ry fetch origin main; git -C ~/.meiva/memory worktree add -q -B mem/dream-$(date +%m%d-%H"
        "%M) $W origin/main; mkdir -p $FH; ln -sfn $W $FH/memory; cd $W`; "
        "2) Phase 1 (Orientation): scan `MEMORY.md` and directory `index.md` files in `$W` to map "
        "topic layout, canonical filenames, and size/entry caps; "
        "3) Phase 2 (Gather Signal): collect three incremental signals — (a) recent session transc"
        "ripts (`USER_INPUT` user corrections, rulings, and reversals), (b) World Truth Probes acr"
        "oss multi-repo codebases/file trees (`git log`, path/symbol existence, retired scripts or"
        " contracts), cluster DB runtime state (`agents`, `schedules`, `tasks` active vs. terminat"
        "ed/retired status), and `<!-- fact-check: ... -->` directives, and (c) per-agent `~/.meiv"
        "a/workspaces/*/memory/` notes; "
        "4) Phase 3 (Consolidation): maintain one dateless canonical file per rule/topic slot, ove"
        "rwrite canonical notes in place with current truth, `git rm` superseded or fragmented dat"
        "e-suffixed notes, and convert relative time words into explicit `YYYY-MM-DD` dates; "
        "5) Phase 4 (Prune & Index): keep `MEMORY.md` zero-conflict and concise under the 16000-ch"
        "ar cap, then run `MEIVA_HOME=$FH ~/.meiva/source/.venv/bin/python ~/.meiva/source/meiva_b"
        "uiltins/plugins/meiva_memory/skills/scripts/rebuild_memory_index.py; python3 validate.py`"
        " (`rebuild_memory_index.py` resolves the pool from MEIVA_HOME and takes no arguments; the"
        " fake home points it at the worktree), commit + push that branch + open a PR and squash-m"
        "erge it once `validate` is green; finally clean up so the registry does not grow one entr"
        "y per night: `git -C ~/.meiva/memory worktree remove --force $W; git -C ~/.meiva/memory w"
        "orktree prune; rm -rf $(dirname $W)`, merge any remaining review PRs whose `validate` che"
        "ck is green and whose author is not the merger (writing the collector snapshot — each fac"
        "e's HEAD + dirty count — into the PR body first), and run `meiva memory refresh`",
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
