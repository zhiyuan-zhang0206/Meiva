# meiva:note
# A registrar's heartbeat product for a fake machine, so registration tests stay about registering:
# the declarations themselves are asserted in `test_job_heartbeat.py`.

from __future__ import annotations

from datetime import date

from base.host.system.cron import heartbeat_population
from base.host.system.job_values import HeartbeatRender, JobRoots, JobSchedule

# meiva:note
# The fake machine these tests render for: an owner id, a name and a fixed day, so an artifact
# line is reproducible and the exception windows below never expire.
MACHINE = "macmini"
UID = 501
TODAY = date(2026, 11, 8)


def heartbeat_for(job: str, roots: JobRoots, *, schedule: JobSchedule) -> HeartbeatRender:
    return heartbeat_population.verified(
        job, roots=roots, machine=MACHINE, uid=UID, schedule=schedule, today=TODAY
    )
