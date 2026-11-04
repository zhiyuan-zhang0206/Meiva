from __future__ import annotations

import shlex
from pathlib import Path
from xml.sax.saxutils import escape

from base.host.system.job_values import (
    LAUNCHD_LABEL_PREFIX,
    JobPaths,
    JobRoots,
    cron_environment,
    launchd_environment,
)

__all__ = [
    "FAMILY_DAYS",
    "HOUR",
    "LABEL",
    "MARKER",
    "MINUTE",
    "cron_entry",
    "launchd_plist_content",
    "log_path",
    "shell_command",
]


# meiva:note
# Every log family keeps seven days.
#
# Tiering kept agent logs 15 days and gateway/ops/watchdog 30, on the assumption that those
# families were worth more and cost less. A measurement on 2026-11-05 says otherwise: the
# directory held 3.62 GiB across 3358 files, and nothing in the long-retention families still
# held bytes past seven days - running with seven days deleted 85 files and 6.2 MiB. What sets
# the directory's size is its generation rate (~520 MiB/day), not the retention window. The user
# chose seven from a 7/14 choice: a full working week, weekends included. Collapsing the tiers
# also leaves one answer to "how long do we keep logs" instead of seven.
FAMILY_DAYS = "agent=7,shell=7,gateway=7,ops=7,watchdog=7,snapshot=7,other=7"

MARKER = "# meiva-logs-maintenance"

HOUR = 4

MINUTE = 40

LABEL = f"{LAUNCHD_LABEL_PREFIX}.logs-maintenance"


def log_path(roots: JobRoots) -> Path:
    return roots.home / "logs" / "logs-maintenance.out.log"


def shell_command(paths: JobPaths) -> str:
    meiva = shlex.quote(paths.meiva)
    return f"{meiva} logs rotate && {meiva} logs retention --family-days {FAMILY_DAYS}"


def launchd_plist_content(*, paths: JobPaths, path_env: str) -> str:
    log_file = escape(str(log_path(paths.roots)))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>-c</string>
        <string>{escape(shell_command(paths))}</string>
    </array>
{launchd_environment(home=paths.roots.home, path_env=path_env, indent="    ", extra=())}
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


def cron_entry(paths: JobPaths) -> str:
    return f"{MINUTE} {HOUR} * * * {cron_environment(paths.roots.home)}/bin/sh -c {shlex.quote(shell_command(paths))}  {MARKER}"
