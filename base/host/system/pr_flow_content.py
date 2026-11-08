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


HOUR = 0

MINUTE = 25

MARKER = "# meiva-pr-flow"

LABEL = f"{LAUNCHD_LABEL_PREFIX}.pr-flow"


# meiva:note
# The sampler has a telemetry consumer but no one watching it: a stopped run leaves the trailing
# window frozen on the dashboard, which looks like a quiet week. Six hours after a missed 00:25
# is the window; the run itself takes minutes, so lateness cannot be its own cause.
HEARTBEAT = JobHeartbeat(name="pr-flow", owner="agent:405", late_after_seconds=6 * 60 * 60)


# meiva:note
# The cadence the plist trigger and the heartbeat declaration both come from: one value, so the
# definition and what it promises cannot describe different schedules.
def schedule() -> JobSchedule:
    return DailyAt(hour=HOUR, minute=MINUTE)


def log_path(roots: JobRoots) -> Path:
    return roots.home / "logs" / "pr-flow.out.log"


def shell_command(paths: JobPaths) -> str:
    return f"{shlex.quote(str(Path(paths.meiva).parent / 'python'))} {shlex.quote(str(paths.roots.repo / 'scripts' / 'ci' / 'pull_requests' / 'pr_flow_export.py'))}"


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
    log = shlex.quote(str(log_path(paths.roots)))
    return f"{MINUTE} {HOUR} * * * {environment}/bin/sh -c {shlex.quote(shell_command(paths))} >> {log} 2>&1  # meiva-heartbeat: {declared.artifact}  {MARKER}"
