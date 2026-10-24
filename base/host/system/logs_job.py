"""Daily OS job for copytruncate rotation followed by tiered retention.

The host registers one local maintenance schedule. POSIX runs both commands
in one shell so retention starts only after rotation succeeds.
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

import base.host.system.cron
from base.host.system import logs_content as content
from base.host.system.hostport import Host
from base.host.system.job_readings import read_launchd_path, read_meiva_binary
from base.host.system.job_values import JobPaths, JobRoots, plist_path


def launchd_plist_path() -> Path:
    return plist_path(content.LABEL, user_home=Path.home())


def register_macos(*, roots: JobRoots, host: Host) -> int:
    paths = JobPaths(roots, read_meiva_binary(roots.repo, host=host))
    path_env = read_launchd_path(paths.meiva, user_home=roots.user_home, host=host)
    label = content.LABEL
    destination = plist_path(content.LABEL, user_home=roots.user_home)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        content.launchd_plist_content(paths=paths, path_env=path_env), encoding="utf-8"
    )

    if base.host.system.cron.reload_launchd_job(label, destination, host=host) != 0:
        return 1
    logger.info(
        "launchd job '{}' loaded (daily at {:02d}:{:02d})", label, content.HOUR, content.MINUTE
    )
    return 0


def unregister_macos(*, host: Host) -> int:
    retired = base.host.system.cron.remove_launchd_job(
        content.LABEL, launchd_plist_path(), host=host
    )
    return 0 if retired else 1


def register_linux(*, roots: JobRoots, host: Host) -> int:
    missing_rc = base.host.system.cron.require_crontab(
        "  * logs maintenance: crontab not installed; daily rotation and "
        "retention cannot be registered",
        missing_returncode=1,
        missing_stream=sys.stderr,
        host=host,
    )
    if missing_rc is not None:
        return missing_rc

    paths = JobPaths(roots, read_meiva_binary(roots.repo, host=host))

    marker = content.MARKER
    entry = content.cron_entry(paths)

    if base.host.system.cron.replace_crontab_entry(
        marker,
        entry,
        skip_phrase="logs-maintenance registration",
        update_failure=lambda err: print(f"  * crontab update failed: {err}", file=sys.stderr),  # noqa: T201
        host=host,
    ):
        return 1
    logger.info("crontab logs-maintenance entry added ({})", marker)
    return 0


def unregister_linux(*, host: Host) -> int:
    return base.host.system.cron.remove_crontab_entry(
        content.MARKER, write_failure_rc=1, on_removed=None, host=host
    )


def register_logs_job(*, enabled: bool, roots: JobRoots, host: Host) -> None:
    if not enabled:
        base.host.system.cron.skip_os_job("logs-maintenance")
        return
    if not base.host.system.cron.owns_os_job_home("logs-maintenance", home=roots.home):
        return
    base.host.system.cron.register_platform_job(
        "logs-maintenance",
        macos=lambda: register_macos(roots=roots, host=host),
        linux=lambda: register_linux(roots=roots, host=host),
        host=host,
    )


def unregister_logs_job(*, host: Host) -> None:
    if not base.host.system.cron.owns_os_jobs("logs-maintenance"):
        return
    base.host.system.cron.unregister_platform_job(
        macos=lambda: unregister_macos(host=host),
        linux=lambda: unregister_linux(host=host),
        host=host,
    )
