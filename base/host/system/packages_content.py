from __future__ import annotations

import shlex
from dataclasses import dataclass
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
    "LABEL",
    "MARKER",
    "RefreshSchedule",
    "cron_entry",
    "launchd_plist_content",
    "log_path",
    "shell_command",
]


MARKER = "# meiva-packages-refresh"

LABEL = f"{LAUNCHD_LABEL_PREFIX}.packages-refresh"


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


def launchd_plist_content(*, tick_seconds: int, paths: JobPaths, path_env: str) -> str:
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


def cron_entry(paths: JobPaths, *, tick_seconds: int) -> str:
    minutes = max(1, tick_seconds // 60)
    return f"*/{minutes} * * * * {cron_environment(paths.roots.home)}/bin/sh -c {shlex.quote(shell_command(paths))} >> {shlex.quote(str(log_path(paths.roots)))} 2>&1  {MARKER}"
