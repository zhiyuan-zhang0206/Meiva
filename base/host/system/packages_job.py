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

import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from loguru import logger

import base.host.system.cron
from base.host.system.hostport import Host

_CRON_MARKER = "# meiva-packages-refresh"


_LABEL = f"{base.host.system.cron.LAUNCHD_LABEL_PREFIX}.packages-refresh"


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_LABEL}.plist"


def _log_file() -> Path:
    return Path(base.host.system.cron.job_home()) / "logs" / "packages-refresh.log"


def _shell_command(*, host: Host) -> str:
    meiva = shlex.quote(base.host.system.cron.meiva_binary_path(host=host))
    return f"{meiva} packages refresh --from-job"


def launchd_plist_content(*, tick_seconds: int, host: Host) -> str:
    log_file = _log_file()
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>-c</string>
        <string>{escape(_shell_command(host=host))}</string>
    </array>
{base.host.system.cron.launchd_env_block(host=host)}
    <key>StartInterval</key>
    <integer>{tick_seconds}</integer>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>{log_file}</string>
    <key>StandardErrorPath</key>
    <string>{log_file}</string>
</dict>
</plist>
"""


def register_macos(*, tick_seconds: int, host: Host) -> int:
    """Rewrite and reload the refresh LaunchAgent."""
    label = _LABEL
    plist_path = launchd_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = _log_file()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(
        launchd_plist_content(tick_seconds=tick_seconds, host=host), encoding="utf-8"
    )

    if base.host.system.cron.reload_launchd_job(label, plist_path, host=host) != 0:
        return 1
    logger.info("launchd job '{}' loaded (every {}s)", label, tick_seconds)
    return 0


def unregister_macos(*, host: Host) -> int:
    retired = base.host.system.cron.remove_launchd_job(_LABEL, launchd_plist_path(), host=host)
    return 0 if retired else 1


def register_linux(*, tick_seconds: int, host: Host) -> int:
    """Replace the refresh line in the user crontab.

    Lines are matched by the marker as a substring.
    """
    missing_rc = base.host.system.cron.require_crontab(
        "  * packages refresh: crontab not installed; the recurring refresh "
        "pass cannot be registered",
        missing_returncode=1,
        missing_stream=sys.stderr,
        host=host,
    )
    if missing_rc is not None:
        return missing_rc

    marker = _CRON_MARKER
    minutes = max(1, tick_seconds // 60)
    log_file = _log_file()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    entry = (
        f"*/{minutes} * * * * {base.host.system.cron.cron_env_prefix()}"
        f"/bin/sh -c {shlex.quote(_shell_command(host=host))} "
        f">> {shlex.quote(str(log_file))} 2>&1  {marker}"
    )
    if base.host.system.cron.replace_crontab_entry(
        marker,
        entry,
        skip_phrase="packages-refresh registration",
        update_failure=lambda err: print(f"  * crontab update failed: {err}", file=sys.stderr),  # noqa: T201
        host=host,
    ):
        return 1
    logger.info("crontab packages-refresh entry added ({})", marker)
    return 0


def unregister_linux(*, host: Host) -> int:
    return base.host.system.cron.remove_crontab_entry(
        _CRON_MARKER, write_failure_rc=1, on_removed=None, host=host
    )


# meiva:note
# The refresh policy a registration needs: whether the channel is switched on and the tick it runs.
#
# The root reads it only for an enabled job in the owning home. A disabled channel has no tick;
# its early exit must not read or validate a scheduler interval it will never use.
@dataclass(frozen=True)
class RefreshSchedule:
    refresh_enabled: bool
    tick_seconds: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.refresh_enabled, bool):
            raise TypeError("the packages refresh switch must be a boolean")
        tick = self.tick_seconds
        if (tick is None and self.refresh_enabled) or (
            tick is not None and (not isinstance(tick, int) or isinstance(tick, bool) or tick <= 0)
        ):
            raise ValueError("registering the packages refresh job needs a positive tick")


# meiva:note
# Register the recurring refresh pass (idempotent).
#
# The owning home needs `schedule` only when enabled, and its tick only for an enabled channel.
# Skipped when OS jobs are off (`MEIVA_OS_JOBS_ENABLED`), when this home is not
# the default home (`owns_os_jobs`) or the refresh channel itself is off — the converge step is the only caller, so a disabled switch
# simply leaves no job behind on machines that never registered one.
def register_packages_job(*, enabled: bool, schedule: RefreshSchedule | None, host: Host) -> None:
    if not enabled:
        base.host.system.cron.skip_os_job("packages refresh")
        return
    if not base.host.system.cron.owns_os_jobs("packages refresh"):
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
    base.host.system.cron.register_platform_job(
        "packages-refresh",
        macos=lambda: register_macos(tick_seconds=tick_seconds, host=host),
        linux=lambda: register_linux(tick_seconds=tick_seconds, host=host),
        host=host,
    )


def unregister_packages_job(*, host: Host) -> None:
    """Remove the recurring refresh pass; safe when none is registered, and a no-op
    outside the default home."""
    if not base.host.system.cron.owns_os_jobs("packages refresh"):
        return
    base.host.system.cron.unregister_platform_job(
        macos=lambda: unregister_macos(host=host),
        linux=lambda: unregister_linux(host=host),
        host=host,
    )
