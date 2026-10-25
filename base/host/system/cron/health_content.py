# meiva:link base/host/docs/health-probe-job.meiva.okf.md

from pathlib import Path
from xml.sax.saxutils import escape

from base.host.system.job_values import LAUNCHD_LABEL_PREFIX, launchd_environment

LABEL = f"{LAUNCHD_LABEL_PREFIX}.health-probe"


def launchd_plist_content(
    *,
    arguments: tuple[str, ...],
    interval_seconds: int,
    log_file: Path,
    environment_home: Path,
    path_env: str,
) -> str:
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
    <key>StartInterval</key>
    <integer>{interval_seconds}</integer>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>{escape(str(log_file))}</string>
    <key>StandardErrorPath</key>
    <string>{escape(str(log_file))}</string>
</dict>
</plist>
"""


def own_probe_job_of(
    own_labels: set[str],
    *,
    current_label: str | None,
    ancestral_labels: frozenset[str] = frozenset(),
) -> str | None:
    if current_label is not None and current_label in own_labels:
        return current_label
    for candidate in sorted(own_labels):
        if candidate in ancestral_labels:
            return candidate
    return None
