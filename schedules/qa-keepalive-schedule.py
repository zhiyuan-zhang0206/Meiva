# meiva:note
# A keeper for one site: it watches an HTTP health page and, optionally, a process on a remote
# host, and wakes the owning agent only after the streak passes the rule in
# `schedules/plans/keepalive_rule.py`.
#
# Everything site-specific comes from the environment -- `MEIVA_QA_KEEPALIVE_URL`,
# `MEIVA_QA_KEEPALIVE_AGENT`, `MEIVA_QA_KEEPALIVE_HOST`, and optionally
# `MEIVA_QA_KEEPALIVE_PROCESS` with `MEIVA_QA_KEEPALIVE_SSH_TARGETS` -- because provisioning
# rewrites a schedule's stored script from this template: a value written here would be forced
# onto every site that runs it. A site that has not configured the keeper stops at its start and
# names the missing variable instead of reporting health it never checked.
# meiva:link schedules/README.md

import os
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from base.paths.home import resolve_meiva_home
from schedules.jobs.entry import cluster_timezone
from schedules.jobs.qa_keepalive import Keepalive, run_keepalive

HINT = (
    "Recovery: 1) reopen the page and check /health; 2) relaunch the inbound watcher; "
    "3) re-arm the page renewal watcher. This message will not repeat for an hour."
)
BLIND_HINT = (
    "The keeper could not reach the host it probes, so it can neither confirm nor deny that the "
    "watcher is alive. Check the ssh target and the network path; this wake repeats hourly."
)


if __name__ == "__main__":
    home = resolve_meiva_home()
    run_keepalive(
        Keepalive(
            env=os.environ,
            state=home / "run" / "qa-keepalive.json",
            zone=ZoneInfo(cluster_timezone()),
            hint=HINT,
            blind_hint=BLIND_HINT,
        ),
        clock=lambda: datetime.now(UTC),
    )
