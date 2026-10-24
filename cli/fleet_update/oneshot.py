# meiva:note
# How a host is started. A Linux host runs `meiva start` over SSH. A macOS host runs it as a
# run-once LaunchAgent in the user's `gui/<uid>` domain, because the permissions helper's launchd
# job lives there and an SSH session cannot load it. The script submits the job, waits for its
# `ONESHOT_RC=` line, prints the job's log and removes the job.
# meiva:link cli/docs/fleet-update.meiva.okf.md

from __future__ import annotations

import plistlib
import re

from .plan.host_commands import start_command


# meiva:note
# The shell that runs `meiva start` as a macOS one-shot LaunchAgent. The label carries the host
# alias and the window's stamp, so two hosts' jobs, or two windows', never share a job directory.
def gui_oneshot(alias: str, stamp: str, timeout_s: int, start_flags: str) -> str:
    label = f"com.meiva.fleet-update.{re.sub(r'[^A-Za-z0-9.-]', '-', alias)}.{stamp}"
    log = f"/tmp/{label}/oneshot.log"
    start = start_command(start_flags)
    job = {
        "Label": label,
        "ProgramArguments": ["/bin/zsh", "-lc", f'({start}); echo "ONESHOT_RC=$?"'],
        "RunAtLoad": True,
        "KeepAlive": False,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }
    return f"""D=/tmp/{label}; mkdir -m 700 "$D" && : > "{log}" || exit 70
cat > "$D/job.plist" <<'MEIVA_FLEET_PLIST'
{plistlib.dumps(job).decode().rstrip()}
MEIVA_FLEET_PLIST
launchctl bootstrap "gui/$(id -u)" "$D/job.plist" || {{ echo "not logged in to the GUI?"; exit 71; }}
n=0; until grep -q '^ONESHOT_RC=' "{log}"; do
  [ "$n" -ge {timeout_s // 5} ] && {{ echo "still running: gui/$(id -u)/{label}, {log}"; exit 72; }}
  sleep 5; n=$((n+1)); done
cat "{log}"; rc=$(sed -n 's/^ONESHOT_RC=//p' "{log}" | tail -n 1)
launchctl bootout "gui/$(id -u)/{label}" || echo "bootout of {label} failed"
rm -rf "$D"; exit "$rc\""""


# meiva:note
# The command that starts one host with the operator's explicit start selection: the one-shot
# LaunchAgent on macOS, the plain start elsewhere. Never `--boot`: a boot attempt spends the boot
# budget, and on macOS the failing attempt that spends it exits 0.
def start_script(*, macos: bool, alias: str, stamp: str, timeout_s: int, flags: str) -> str:
    return gui_oneshot(alias, stamp, timeout_s, flags) if macos else start_command(flags)
