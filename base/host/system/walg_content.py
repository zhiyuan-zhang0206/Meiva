from __future__ import annotations

import shlex
from pathlib import Path
from xml.sax.saxutils import escape

from base.host.system.job_values import (
    LAUNCHD_LABEL_PREFIX,
    DailyAt,
    HeartbeatRender,
    JobHeartbeat,
    JobPaths,
    JobRoots,
    JobSchedule,
    MissingHeartbeatDeclaration,
    cron_environment,
    launchd_environment,
)

__all__ = [
    "HEARTBEAT",
    "LABEL",
    "MARKER",
    "MINUTE",
    "cron_entry",
    "hour",
    "launchd_plist_content",
    "log_path",
    "schedule",
    "shell_command",
]


MARKER = "# meiva-walg"

_HOURS_AFTER_LOGICAL_BACKUP = 3

MINUTE = 25

LABEL = f"{LAUNCHD_LABEL_PREFIX}.walg"


# meiva:note
# A backup tick that stops firing is found on the day someone needs a restore, which is the worst
# possible day to find it. Six hours after the missed slot is the window; the tick's own duration
# is minutes, so lateness here is a stopped job rather than a slow one.
HEARTBEAT = JobHeartbeat(name="walg", owner="agent:405", late_after_seconds=6 * 60 * 60)


# meiva:note
# The cadence the plist trigger and the heartbeat declaration both come from: one value, so the
# definition and what it promises cannot describe different schedules.
def schedule(*, backup_hour: int) -> JobSchedule:
    return DailyAt(hour=hour(backup_hour=backup_hour), minute=MINUTE)


def hour(*, backup_hour: int) -> int:
    return (backup_hour + _HOURS_AFTER_LOGICAL_BACKUP) % 24


def log_path(roots: JobRoots) -> Path:
    return roots.home / "logs" / "walg.log"


def shell_command(paths: JobPaths) -> str:
    meiva = shlex.quote(paths.meiva)
    return f"{meiva} backup walg run"


def _heartbeat(heartbeat: HeartbeatRender | None) -> HeartbeatRender:
    if heartbeat is None:
        raise MissingHeartbeatDeclaration(
            f"{LABEL} renders a plist but was handed no heartbeat: a job nobody watches is "
            f"indistinguishable from one that was removed"
        )
    return heartbeat


def launchd_plist_content(
    *,
    backup_hour: int,
    paths: JobPaths,
    path_env: str,
    heartbeat: HeartbeatRender | None = None,
) -> str:
    declared = _heartbeat(heartbeat)
    log_file = escape(str(log_path(paths.roots)))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <!-- {escape(declared.artifact)} -->
    <key>Label</key>
    <string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>-c</string>
        <string>{escape(shell_command(paths))}</string>
    </array>
{launchd_environment(home=paths.roots.home, path_env=path_env, indent="    ", extra=declared.environment)}
    <key>StartCalendarInterval</key>
    <dict>
            <key>Hour</key>
            <integer>{hour(backup_hour=backup_hour)}</integer>
            <key>Minute</key>
            <integer>{MINUTE}</integer>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>{log_file}</string>
    <key>StandardErrorPath</key>
    <string>{log_file}</string>
</dict>
</plist>
"""


def cron_entry(
    paths: JobPaths, *, backup_hour: int, heartbeat: HeartbeatRender | None = None
) -> str:
    declared = _heartbeat(heartbeat)
    environment = cron_environment(paths.roots.home, declared.environment)
    log = shlex.quote(str(log_path(paths.roots)))
    return f"{MINUTE} {hour(backup_hour=backup_hour)} * * * {environment}/bin/sh -c {shlex.quote(shell_command(paths))} >> {log} 2>&1  # meiva-heartbeat: {declared.artifact}  {MARKER}"
