from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from base.host.system.job_values import (
    LAUNCHD_LABEL_PREFIX,
    EverySeconds,
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
    "RefreshSchedule",
    "cron_entry",
    "launchd_plist_content",
    "log_path",
    "schedule",
    "shell_command",
]


MARKER = "# meiva-packages-refresh"

LABEL = f"{LAUNCHD_LABEL_PREFIX}.packages-refresh"


# meiva:note
# This job is the case the heartbeat exists for: it has consumers (the package pull path reads
# what it writes) and no one watching. Stopped for two days it shows up only as versions that
# look old. The grace is four missed ticks of the usual 900s — one tick can be skipped while the
# machine is asleep, four in a row is a job that stopped.
HEARTBEAT = JobHeartbeat(name="packages-refresh", owner="agent:405", late_after_seconds=4 * 15 * 60)


# meiva:note
# The cadence the plist trigger and the heartbeat declaration both come from: one value, so the
# definition and what it promises cannot describe different schedules.
def schedule(*, tick_seconds: int) -> JobSchedule:
    return EverySeconds(seconds=tick_seconds)


# meiva:note
# The root reads this policy only after global/home admission. A disabled channel
# has no tick; it must not validate an interval the scheduler will never use.
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


def log_path(roots: JobRoots) -> Path:
    return roots.home / "logs" / "packages-refresh.log"


def shell_command(paths: JobPaths) -> str:
    meiva = shlex.quote(paths.meiva)
    return f"{meiva} packages refresh --from-job"


def _heartbeat(heartbeat: HeartbeatRender | None) -> HeartbeatRender:
    if heartbeat is None:
        raise MissingHeartbeatDeclaration(
            f"{LABEL} renders a plist but was handed no heartbeat: a job nobody watches is "
            f"indistinguishable from one that was removed"
        )
    return heartbeat


def launchd_plist_content(
    *,
    tick_seconds: int,
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


def cron_entry(
    paths: JobPaths, *, tick_seconds: int, heartbeat: HeartbeatRender | None = None
) -> str:
    declared = _heartbeat(heartbeat)
    minutes = max(1, tick_seconds // 60)
    environment = cron_environment(paths.roots.home, declared.environment)
    log = shlex.quote(str(log_path(paths.roots)))
    return f"*/{minutes} * * * * {environment}/bin/sh -c {shlex.quote(shell_command(paths))} >> {log} 2>&1  # meiva-heartbeat: {declared.artifact}  {MARKER}"
