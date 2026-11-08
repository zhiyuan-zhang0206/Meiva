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
    "HOUR",
    "LABEL",
    "MARKER",
    "MINUTE",
    "cron_entry",
    "launchd_plist_content",
    "log_path",
    "schedule",
    "shell_command",
]


MARKER = "# meiva-logs-maintenance"

HOUR = 4

MINUTE = 40

LABEL = f"{LAUNCHD_LABEL_PREFIX}.logs-maintenance"


# meiva:note
# The job's promise to the machine, declared beside the trigger it renders: it writes
# `logs-maintenance.json` when it finishes, and six hours after a missed 04:40 someone is called.
# The window is generous because the job is a once-a-day maintenance pass — an hour of lateness
# is a slow disk, not a job that stopped, and a false alarm costs the owner's attention.
HEARTBEAT = JobHeartbeat(name="logs-maintenance", owner="agent:405", late_after_seconds=6 * 60 * 60)


# meiva:note
# The cadence the plist trigger and the heartbeat declaration both come from: one value, so the
# definition and what it promises cannot describe different schedules.
def schedule() -> JobSchedule:
    return DailyAt(hour=HOUR, minute=MINUTE)


def log_path(roots: JobRoots) -> Path:
    return roots.home / "logs" / "logs-maintenance.out.log"


# meiva:note
# The job names behaviour, not policy.
#
# It used to spell the tiers out (`--family-days agent=15,...`), which made every
# generated plist a copy of a decision that belongs to the operator. A converge run from
# an older install then rewrote the plist with its own copy: on 2026-11-05 an update
# reverted a seven-day policy eight minutes after it was applied. `logs maintenance`
# reads `MEIVA_LOG_FAMILY_DAYS` when it runs, so the value lives in one place that no
# release carries.
#
# The trade-off is a version skew: a CLI too old to know `logs maintenance` exits
# non-zero and the job stops visibly (launchd `last exit code` is not 0) instead of
# silently applying the wrong tiers.
def shell_command(paths: JobPaths) -> str:
    meiva = shlex.quote(paths.meiva)
    return f"{meiva} logs maintenance"


def _heartbeat(heartbeat: HeartbeatRender | None) -> HeartbeatRender:
    if heartbeat is None:
        raise MissingHeartbeatDeclaration(
            f"{LABEL} renders a plist but was handed no heartbeat: a job nobody watches is "
            f"indistinguishable from one that was removed"
        )
    return heartbeat


def launchd_plist_content(
    *, paths: JobPaths, path_env: str, heartbeat: HeartbeatRender | None = None
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
            <integer>{HOUR}</integer>
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


def cron_entry(paths: JobPaths, *, heartbeat: HeartbeatRender | None = None) -> str:
    declared = _heartbeat(heartbeat)
    environment = cron_environment(paths.roots.home, declared.environment)
    return f"{MINUTE} {HOUR} * * * {environment}/bin/sh -c {shlex.quote(shell_command(paths))}  # meiva-heartbeat: {declared.artifact}  {MARKER}"
