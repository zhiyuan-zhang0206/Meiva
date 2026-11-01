"""Recurring OS job for the content-refresh pass (design §5.6; task #3267).

One schedule per host runs `meiva packages refresh --from-job` every
`MEIVA_PACKAGES_REFRESH_TICK_SECONDS` (default 900s). The command owns all the
gating (the jobs/refresh switches, a cluster update in flight, the per-home
flock, per-package cadence and backoff), so the job spec stays dumb and
idempotent — one tick, no re-registration when a policy changes.

POSIX surfaces the run's output in `$MEIVA_HOME/logs/packages-refresh.log`
(launchd appends stdout/stderr; the crontab line redirects).
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from base.host.system import packages_content as content
from base.host.system.cron import mechanics
from base.host.system.hostport import Host
from base.host.system.job_readings import read_launchd_path, read_meiva_binary
from base.host.system.job_values import JobPaths, JobRoots, plist_path


def register_macos(*, tick_seconds: int, roots: JobRoots, host: Host) -> int:
    paths = JobPaths(roots, read_meiva_binary(roots.repo, host=host))
    path_env = read_launchd_path(paths.meiva, user_home=roots.user_home, host=host)
    label = content.LABEL
    destination = plist_path(content.LABEL, user_home=roots.user_home)
    destination.parent.mkdir(parents=True, exist_ok=True)
    log_file = content.log_path(roots)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        content.launchd_plist_content(tick_seconds=tick_seconds, paths=paths, path_env=path_env),
        encoding="utf-8",
    )

    if mechanics.reload_launchd_job(label, destination, host=host) != 0:
        return 1
    logger.info("launchd job '{}' loaded (every {}s)", label, tick_seconds)
    return 0


def unregister_macos(*, host: Host, user_home: Path) -> int:
    retired = mechanics.remove_launchd_job(
        content.LABEL, plist_path(content.LABEL, user_home=user_home), host=host
    )
    return 0 if retired else 1


def register_linux(*, tick_seconds: int, roots: JobRoots, host: Host) -> int:
    missing_rc = mechanics.require_crontab(
        "  * packages refresh: crontab not installed; the recurring refresh "
        "pass cannot be registered",
        missing_returncode=1,
        missing_stream=sys.stderr,
        host=host,
    )
    if missing_rc is not None:
        return missing_rc

    paths = JobPaths(roots, read_meiva_binary(roots.repo, host=host))

    marker = content.MARKER
    log_file = content.log_path(roots)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    entry = content.cron_entry(paths, tick_seconds=tick_seconds)

    if mechanics.replace_crontab_entry(
        marker,
        entry,
        skip_phrase="packages-refresh registration",
        update_failure=lambda err: print(f"  * crontab update failed: {err}", file=sys.stderr),  # noqa: T201
        user_home=roots.user_home,
        host=host,
    ):
        return 1
    logger.info("crontab packages-refresh entry added ({})", marker)
    return 0


def unregister_linux(*, roots: JobRoots, host: Host) -> int:
    return mechanics.remove_crontab_entry(
        content.MARKER, write_failure_rc=1, on_removed=None, user_home=roots.user_home, host=host
    )


# meiva:note
# Register the recurring refresh pass (idempotent).
#
# The owning home needs `schedule` only when enabled, and its tick only for an enabled channel.
# Skipped when OS jobs are off (`MEIVA_OS_JOBS_ENABLED`), when this home is not
# the default home (`owns_os_job_home`) or the refresh channel itself is off — the converge step is the only caller, so a disabled switch
# simply leaves no job behind on machines that never registered one.
def register_packages_job(
    *, enabled: bool, schedule: content.RefreshSchedule | None, roots: JobRoots, host: Host
) -> None:
    if not enabled:
        mechanics.skip_os_job("packages refresh")
        return
    if not mechanics.owns_os_job_home("packages refresh", roots=roots):
        return
    if schedule is None:
        raise ValueError("registering the packages refresh job needs its schedule")
    if not schedule.refresh_enabled:
        logger.info(
            "packages refresh disabled (MEIVA_PACKAGES_REFRESH_ENABLED=false) — not registering"
        )
        return
    tick_seconds = schedule.tick_seconds
    if tick_seconds is None:
        raise ValueError("registering the packages refresh job needs its tick")
    mechanics.register_platform_job(
        "packages-refresh",
        macos=lambda: register_macos(tick_seconds=tick_seconds, roots=roots, host=host),
        linux=lambda: register_linux(tick_seconds=tick_seconds, roots=roots, host=host),
        host=host,
    )


def unregister_packages_job(*, host: Host, roots: JobRoots) -> None:
    if not mechanics.owns_os_job_home("packages refresh", roots=roots):
        return
    mechanics.unregister_platform_job(
        macos=lambda: unregister_macos(host=host, user_home=roots.user_home),
        linux=lambda: unregister_linux(roots=roots, host=host),
        host=host,
    )
