# meiva:note
# OS cron registration for the cluster health probe: `meiva cluster health-probe-register`
# and `meiva cluster health-probe-unregister`.
#
# Thin CLI wrappers that delegate to `base.host.system.cron`. The core logic lives in the base
# layer so both the gateway lifespan (primary registration path) and the CLI converge step
# (belt-and-suspenders fallback) can call the same functions without violating the import
# layering.

from __future__ import annotations

from base.host.system.cron import (
    DEFAULT_INTERVAL_SECONDS,
    register_os_cron,
    unregister_os_cron,
)
from base.host.system.hostport import Host


# meiva:note
# Register the OS cron job for the cluster health probe.
#
# Delegates to `base.host.system.cron.register_os_cron`, which takes its gate as a reader; the
# value `os_jobs_enabled` was read once by the composition root. Idempotent: re-running
# updates the interval and reloads the job. A refusal exits 1 with the reason.
def cmd_cron_register(
    *, os_jobs_enabled: bool, host: Host, interval_s: int = DEFAULT_INTERVAL_SECONDS
) -> int:
    try:
        register_os_cron(interval_s=interval_s, enabled_reader=lambda: os_jobs_enabled, host=host)
    except RuntimeError as e:
        print(f"  * {e}")
        return 1
    return 0


# meiva:note
# Remove the OS cron job for the cluster health probe; delegates to
# `base.host.system.cron.unregister_os_cron`.
def cmd_cron_unregister(*, host: Host) -> int:
    try:
        unregister_os_cron(host=host)
    except RuntimeError as e:
        print(f"  * {e}")
        return 1
    return 0
