# meiva:note
# Direct sentinel for a stalled database write path. PgBouncer's per-minute
# stats line is the earliest host-local evidence that transactions stopped
# completing; over 29 days of history the rule in `schedules/plans/stall_rule.py` (three
# consecutive minutes under 20 xacts/s with an average query time above 300 ms) marked only the
# two real incidents and no quiet night. It is the root-cause signal that the
# agent-host crash wave is the consequence of.
# meiva:link schedules/README.md

from zoneinfo import ZoneInfo

from base.paths.home import resolve_meiva_home
from schedules.jobs.catchup import cluster_timezone
from schedules.jobs.stall_watch import StallWatch, run_watch

REPORT_AGENTS = (1818, 405)


if __name__ == "__main__":
    home = resolve_meiva_home()
    run_watch(
        StallWatch(
            log=home / "pgbouncer" / "pgbouncer.log",
            state=home / "run" / "db-write-stall-watch.json",
            zone=ZoneInfo(cluster_timezone()),
            report_agents=REPORT_AGENTS,
        )
    )
