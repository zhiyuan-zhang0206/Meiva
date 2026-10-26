# meiva:note
# OS cron registration for the cluster health probe: `meiva cluster health-probe-register`
# and `meiva cluster health-probe-unregister`.
#
# Thin CLI wrappers that delegate to `base.host.system.cron.health_job`. The core logic lives in the base
# layer so both the gateway lifespan (primary registration path) and the CLI converge step
# (belt-and-suspenders fallback) can call the same functions without violating the import
# layering.

from __future__ import annotations

from base.host.system.cron.health_job import (
    DEFAULT_INTERVAL_SECONDS,
    register_os_cron,
    unregister_os_cron,
)
from base.host.system.hostport import Host
from base.host.system.job_values import JobRoots


# meiva:note
# Register the OS cron job for the cluster health probe.
#
# Delegates to `base.host.system.cron.health_job.register_os_cron`, which takes its gate as a value: the
# `os_jobs_enabled` the composition root read once. Idempotent: re-running
# updates the interval and reloads the job. A refusal exits 1 with the reason.
def cmd_cron_register(
    *,
    os_jobs_enabled: bool,
    host: Host,
    roots: JobRoots,
    interval_s: int = DEFAULT_INTERVAL_SECONDS,
) -> int:
    try:
        register_os_cron(interval_s=interval_s, enabled=os_jobs_enabled, host=host, roots=roots)
    except RuntimeError as e:
        print(f"  * {e}")
        return 1
    return 0


# meiva:note
# Remove the OS cron job for the cluster health probe; delegates to
# `base.host.system.cron.health_job.unregister_os_cron`.
def cmd_cron_unregister(*, host: Host, roots: JobRoots) -> int:
    try:
        unregister_os_cron(host=host, roots=roots)
    except RuntimeError as e:
        print(f"  * {e}")
        return 1
    return 0
