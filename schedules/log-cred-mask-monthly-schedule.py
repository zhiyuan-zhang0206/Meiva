# meiva:note
# `log-cred-mask-monthly` -- on the 15th at 10:00 cluster time, run the log-credential hygiene sweep
# across the five-machine set (approved by 405 on 2026-09-30, follow-up to the masked-backup-20260920
# TTL closeout): fresh strict scan, same-length credential masking (`MASKED-*`), strict recount,
# final dry run.
#
# This schedule owns cadence and the red path only; the sweep itself stays a host-local operator
# tool and is resolved from the runtime home rather than a literal path, so the template is not
# pinned to one machine. A missing tool is a tool error (rc=2) and is reported, never a silent
# green: "nothing to check" and "could not check" must not look alike.
#
# Contract: cron `0 10 15 * *`, at-most-once slots (a slot missed while the schedule is down is
# skipped, not caught up); one `RESULT` line plus the sweep's tail goes to
# `$MEIVA_HOME/logs/log-cred-mask-monthly.log`; the operator is woken only on rc != 0 (residual
# post>0 => 1, tool error => 2) and a green run stays silent. Modes: (default) resident; `--once`
# runs one sweep now; `--no-notify` silences the red path for self-tests.
#
# This file is the repository template; `meiva schedules provision` writes its text into the stored
# row, and the runner materializes that copy into the schedule's own directory.
# meiva:link schedules/README.md

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from zoneinfo import ZoneInfo

import meiva
from base.daemon.schedules.watcher import next_fire
from base.paths import logs_dir
from base.paths.home import resolve_meiva_home
from schedules.jobs.catchup import cluster_timezone
from schedules.jobs.entry import open_schedule_entry
from schedules.jobs.loop import CronSchedule, Trigger, run_loop
from schedules.plans.loop_plan import LoopTiming
from schedules.plans.schedule_identity import parse_schedule_id

TARGET_AGENT = 1818

CRON = "0 10 15 * *"

TRIGGERS = [(CRON, "Monthly log-credential sweep")]

LOG_NAME = "log-cred-mask-monthly.log"

SWEEP_RELATIVE = Path("log-cred-mask") / "monthly_sweep.py"

SWEEP_TIMEOUT_S = 3600


# meiva:note
# The sweep is addressed relative to the runtime home of the host that runs this schedule, so the
# same stored text works on whichever machine owns the schedule manager.
def _sweep_path(home: Path) -> Path:
    return home / SWEEP_RELATIVE


# meiva:note
# Append one run's lines; a log that cannot be written is stderr noise, never a failed sweep.
def _log(lines: list[str], *, home: Path) -> None:
    try:
        with (logs_dir(home=home) / LOG_NAME).open("a", encoding="utf-8") as handle:
            for line in lines:
                handle.write(line + "\n")
    except OSError as exc:
        print("log write failed: %s" % exc, file=sys.stderr)


# meiva:note
# The schedule's own actor sends through the SDK; a bare manual process has none, so the tool's
# system actor is established first.
def _notify(message: str) -> None:
    from meiva import agent_identity

    try:
        agent_identity.require_actor()
    except RuntimeError:
        agent_identity.establish_actor("system:log-cred-mask")
    meiva.agents.send_message(TARGET_AGENT, message)


# meiva:note
# The scanner is a five-machine operator tool that lives under the runtime home of the machine that
# runs the schedule manager; it is not installed anywhere else and does not ship in this repository.
# So a fire first confirms the tool is where this file says it is: on a host without it the run ends
# as one explicit rc=2 line naming the missing path, never as a silent retry of a command that
# cannot succeed, and never as a green "nothing to check".
def _missing_tool(home: Path) -> str | None:
    path = _sweep_path(home)
    return None if path.is_file() else "no credential scanner at %s on this machine" % path


# meiva:note
# One sweep: run the tool as a child under this interpreter, keep the last lines of its output as
# the run's evidence, and treat every failure to run it as rc=2. The red path reports the sweep's
# own tail plus ours, so the wake carries why the sweep said no.
def run_sweep(*, home: Path, notify: bool = True) -> tuple[int, list[str]]:
    stamp = datetime.now(ZoneInfo(cluster_timezone())).isoformat(timespec="seconds")
    missing = _missing_tool(home)
    if missing is not None:
        rc, tail, err = 2, [], missing
    else:
        try:
            proc = subprocess.run(
                [sys.executable, str(_sweep_path(home))],
                capture_output=True,
                text=True,
                timeout=SWEEP_TIMEOUT_S,
                check=False,
            )
        except Exception as exc:
            rc, tail, err = 2, [], "%s: %s" % (type(exc).__name__, exc)
        else:
            rc = proc.returncode
            tail = (proc.stdout or "").strip().splitlines()[-7:]
            err = (proc.stderr or "").strip()[-200:].replace("\n", " ")
    lines = ["RESULT ts=%s rc=%s sweep=%s" % (stamp, rc, _sweep_path(home).name)]
    lines.extend(tail)
    if err:
        lines.append("ERR " + err)
    _log(lines, home=home)
    if rc != 0 and notify:
        message = "[log-cred-mask] rc=%s (ts=%s)\n%s\nlog: %s" % (
            rc,
            stamp,
            "\n".join(tail[-5:] + ([err] if err else [])),
            logs_dir(home=home) / LOG_NAME,
        )
        try:
            meiva.ensure_plugins_loaded()
            _notify(message)
        except Exception as exc:
            print("sdk notify failed: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
            cli = Path(sys.executable).parent / "meiva"
            if cli.is_file():
                try:
                    subprocess.run(
                        [
                            str(cli),
                            "agents",
                            "send",
                            "--source",
                            "system:log-cred-mask",
                            str(TARGET_AGENT),
                            message,
                        ],
                        capture_output=True,
                        text=True,
                        timeout=60,
                        check=False,
                    )
                except Exception as exc2:
                    print(
                        "cli notify failed: %s: %s" % (type(exc2).__name__, exc2), file=sys.stderr
                    )
    return rc, lines


def _fire(_slot: datetime, message: str | None, *, home: Path) -> None:
    rc, lines = run_sweep(home=home, notify=True)
    print("[log-cred-mask-monthly] %s (rc=%s)" % (message, rc))
    for ln in lines[:8]:
        print("[log-cred-mask-monthly] " + ln)


# meiva:note
# The next-fire line is an observation point, not decoration: `run_loop` never prints when it will
# fire next, this schedule is monthly and silent when green, so without the line there is no way to
# tell "computed correctly and waiting" from "computed wrongly and will never fire".
def main(home: Path) -> None:
    with open_schedule_entry(globals().get("MEIVA_SCHEDULE_INPUTS"), home=home) as inputs:
        print(
            "[log-cred-mask-monthly] next fire %s (cron %s)"
            % (
                next_fire(CRON, after=datetime.now(UTC), timezone=cluster_timezone()).isoformat(),
                CRON,
            )
        )
        run_loop(
            inputs.database(),
            CronSchedule(
                tuple(Trigger(expr, payload) for expr, payload in TRIGGERS),
                timezone=cluster_timezone(),
                timing=LoopTiming(),
            ),
            partial(_fire, home=home),
            schedule_id=parse_schedule_id(os.environ.get("MEIVA_SCHEDULE_ID")),
            started_at=datetime.now(UTC),
        )


if __name__ == "__main__":
    _home = resolve_meiva_home()
    if "--once" in sys.argv:
        _rc, _lines = run_sweep(home=_home, notify="--no-notify" not in sys.argv)
        for _ln in _lines:
            print(_ln)
        sys.exit(_rc)
    if not os.environ.get("MEIVA_SCHEDULE_ID"):
        print(
            "no MEIVA_SCHEDULE_ID: this file runs as a gateway schedule; use --once for a manual run",
            file=sys.stderr,
        )
        sys.exit(2)
    main(_home)
