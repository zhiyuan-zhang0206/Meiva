# meiva:note
# Direct sentinel for a stalled database write path. PgBouncer's per-minute
# stats line is the earliest host-local evidence that transactions stopped
# completing; over 29 days of history the rule in `schedules/plans/watch/stall_rule.py` (three
# consecutive minutes under 20 xacts/s with an average query time above 300 ms) marked only the
# two real incidents and no quiet night. It is the root-cause signal that the
# agent-host crash wave is the consequence of.
# meiva:link schedules/README.md

import shutil
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from base.paths.home import resolve_meiva_home
from schedules.jobs.entry import cluster_timezone, open_schedule_entry
from schedules.jobs.watch.stall_watch import StallWatch, run_watch_once

REPORT_AGENTS = (1818, 405)


def _state_path(home: Path) -> Path:
    current = Path("state.json")
    legacy = home / "run" / "db-write-stall-watch.json"
    if not current.exists() and legacy.is_file():
        shutil.copy2(legacy, current)
    return current


if __name__ == "__main__":
    home = resolve_meiva_home()
    with open_schedule_entry(globals().get("MEIVA_SCHEDULE_INPUTS"), home=home):
        run_watch_once(
            StallWatch(
                log=home / "pgbouncer" / "pgbouncer.log",
                state=_state_path(home),
                history=home / "state" / "db-write-stall-history.jsonl",
                zone=ZoneInfo(cluster_timezone()),
                report_agents=REPORT_AGENTS,
                home=home,
            ),
            clock=time.time,
        )
