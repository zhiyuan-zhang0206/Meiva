"""Daily OS job for the WAL-G physical backup (`meiva backup walg run`).

One schedule per host runs the daily tick: backup, verify the archived chain,
apply retention. The command owns every gate (the key switch, a deploy window,
Postgres reachability, the per-home lock), so the job spec stays a single dumb,
idempotent line: no re-registration when a policy changes.

The time of day is a scheduling choice, not a comparison threshold: it sits
`_HOURS_AFTER_LOGICAL_BACKUP` hours after the daily logical dump becomes due
(`services.backup_hour`) so the dump and its Sunday restore drill have finished
before a base backup starts reading the same disks, and it is not the 04:40 log
maintenance slot. Which hour that is follows the dump's hour; a changed
`MEIVA_BACKUP_HOUR` is picked up by the next converge.

POSIX surfaces the run's output in `$MEIVA_HOME/logs/walg.log` (launchd appends
stdout/stderr; the crontab line redirects).
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from base.host.system import walg_content as content
from base.host.system.cron import mechanics
from base.host.system.hostport import Host
from base.host.system.job_readings import read_launchd_path, read_meiva_binary
from base.host.system.job_values import JobPaths, JobRoots, plist_path


def launchd_plist_path(*, user_home: Path | None = None) -> Path:
    return mechanics.user_plist_path(content.LABEL, user_home=user_home)


def register_macos(*, backup_hour: int, roots: JobRoots, host: Host) -> int:
    paths = JobPaths(roots, read_meiva_binary(roots.repo, host=host))
    path_env = read_launchd_path(paths.meiva, user_home=roots.user_home, host=host)
    destination = plist_path(content.LABEL, user_home=roots.user_home)
    destination.parent.mkdir(parents=True, exist_ok=True)
    content.log_path(roots).parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        content.launchd_plist_content(backup_hour=backup_hour, paths=paths, path_env=path_env),
        encoding="utf-8",
    )

    if mechanics.reload_launchd_job(content.LABEL, destination, host=host) != 0:
        return 1
    logger.info(
        "launchd job '{}' loaded (daily at {:02d}:{:02d})",
        content.LABEL,
        content.hour(backup_hour=backup_hour),
        content.MINUTE,
    )
    return 0


def unregister_macos(*, host: Host, user_home: Path | None = None) -> int:
    retired = mechanics.remove_launchd_job(
        content.LABEL, launchd_plist_path(user_home=user_home), host=host
    )
    return 0 if retired else 1


def register_linux(*, backup_hour: int, roots: JobRoots, host: Host) -> int:
    missing_rc = mechanics.require_crontab(
        "  * WAL-G backup: crontab not installed; the daily tick cannot be registered",
        missing_returncode=1,
        missing_stream=sys.stderr,
        host=host,
    )
    if missing_rc is not None:
        return missing_rc

    paths = JobPaths(roots, read_meiva_binary(roots.repo, host=host))

    log_file = content.log_path(roots)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    entry = content.cron_entry(paths, backup_hour=backup_hour)

    if mechanics.replace_crontab_entry(
        content.MARKER,
        entry,
        skip_phrase="WAL-G tick registration",
        update_failure=lambda err: print(f"  * crontab update failed: {err}", file=sys.stderr),  # noqa: T201
        host=host,
    ):
        return 1
    logger.info("crontab WAL-G tick entry added ({})", content.MARKER)
    return 0


def unregister_linux(*, host: Host) -> int:
    return mechanics.remove_crontab_entry(
        content.MARKER, write_failure_rc=1, on_removed=None, host=host
    )


# meiva:note
# Register the daily WAL-G tick (idempotent).
#
# An enabled job in the owning home requires `backup_hour`; it reaches the platform unchanged and
# keeps the three-hour offset from the logical dump.
#
# Skipped when OS jobs are off (`MEIVA_OS_JOBS_ENABLED`) or this home is not the
# default home (`owns_os_job_home`). Whether WAL-G is switched on is the converge
# step's decision (`ensure_walg_job`), not this registrar's.
def register_walg_job(
    *, enabled: bool, backup_hour: int | None, roots: JobRoots, host: Host
) -> None:
    if not enabled:
        mechanics.skip_os_job("WAL-G tick")
        return
    if not mechanics.owns_os_job_home("WAL-G tick", home=roots.home):
        return
    if backup_hour is None:
        raise ValueError("registering the WAL-G tick needs the backup hour")
    hour_of_dump = backup_hour
    mechanics.register_platform_job(
        "WAL-G tick",
        macos=lambda: register_macos(backup_hour=hour_of_dump, roots=roots, host=host),
        linux=lambda: register_linux(backup_hour=hour_of_dump, roots=roots, host=host),
        host=host,
    )


def unregister_walg_job(*, host: Host, user_home: Path | None = None) -> None:
    if not mechanics.owns_os_jobs("WAL-G tick"):
        return
    mechanics.unregister_platform_job(
        macos=lambda: unregister_macos(host=host, user_home=user_home),
        linux=lambda: unregister_linux(host=host),
        host=host,
    )
