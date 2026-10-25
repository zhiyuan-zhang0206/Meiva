# meiva:link base/host/docs/boot-autostart.meiva.okf.md

from pathlib import Path
from xml.sax.saxutils import escape

from base.host.system.job_values import LAUNCHD_LABEL_PREFIX, launchd_environment

LABEL = f"{LAUNCHD_LABEL_PREFIX}.autostart"


def launchd_plist_content(
    *,
    arguments: tuple[str, ...],
    retry_interval_seconds: int,
    log_file: Path,
    environment_home: Path,
    path_env: str,
) -> str:
    # meiva:link base/host/docs/boot-autostart.meiva.okf.md
    argument_lines = "\n".join(f"        <string>{escape(arg)}</string>" for arg in arguments)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{argument_lines}
    </array>
{launchd_environment(home=environment_home, path_env=path_env, indent="    ", extra=())}
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>{retry_interval_seconds}</integer>
    <key>StandardOutPath</key>
    <string>{escape(str(log_file))}</string>
    <key>StandardErrorPath</key>
    <string>{escape(str(log_file))}</string>
</dict>
</plist>
"""
