# meiva:note
# `sched-dry-import-weekly` -- every Monday 09:00 cluster time, dry-import every schedule's stored
# script so a stale copy is caught BEFORE a (re)enabled schedule can crash on it (task #4800 (b)).
#
# The failure class this exists for: the repo moves a module and the DB-embedded copies do not roll.
# It has bitten twice -- #2678 (2026-09-17, `list_agents` signature drift; schedule #2's first fire
# failed silently) and R3 wave 2 (2026-09-25, `shared/watcher` -> `shared.daemon.schedules.watcher`;
# ubuntu's in-store copies stayed stale and schedule id 5 crash-looped into auto-pause).
#
# Sweep contract (1818 spec, 2026-09-29): every schedule row, stopped ones included, gets a syntax
# check plus an imports-only execution in the runner's venv -- the sweep never runs a schedule body
# and never fires anything. One summary line and one red line per failure go to
# `$MEIVA_HOME/logs/sched-dry-import.log` (green runs keep the summary line, so "quiet" can never
# be confused with "did not run"). Reds and tool errors are reported to the cluster operator,
# best-effort, as the schedule actor. Exit codes: 0 green, 1 red, 2 tool error.
#
# Modes: (default) resident loop on cron `0 9 * * 1`, skipping slots missed while down, never
# exiting on a sweep error; `--once` runs one sweep now; `--no-notify` silences the red path;
# `--check-file PATH` checks one script file without the database, which is the falsification
# control for the sweep itself.
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

import base
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

CRON = "0 9 * * 1"

TRIGGERS = [(CRON, "Weekly schedules dry-import sweep")]

LOG_NAME = "sched-dry-import.log"

CHILD_TIMEOUT_S = 90

SOURCE_ROOT = Path(base.__file__).resolve().parents[1]


# meiva:note
# The classification runs as its own process (`schedules/jobs/dry_import_child.py`) under the
# runner's interpreter with the checkout as cwd, so a script is never imported into this process
# and one wedged import cannot take the sweep down with it.
DRY_IMPORT_CHILD = SOURCE_ROOT / "schedules" / "jobs" / "dry_import_child.py"


# meiva:note
# Append one run's lines; a log that cannot be written is noise on stderr, never a failed sweep --
# the report to the operator is what has to survive, the file is the archive.
def _log(lines: list[str], *, home: Path) -> None:
    try:
        with (logs_dir(home=home) / LOG_NAME).open("a", encoding="utf-8") as handle:
            for line in lines:
                handle.write(line + "\n")
    except OSError as exc:
        print("log write failed: %s" % exc, file=sys.stderr)


# meiva:note
# The schedule's own actor sends through the SDK; a bare manual process has none, so the tool's
# system actor is established first instead of letting the send fail for lack of one.
def _send_sdk(message: str) -> None:
    from meiva import agent_identity

    try:
        agent_identity.require_actor()
    except RuntimeError:
        agent_identity.establish_actor("system:sched-dry-import")
    meiva.agents.send_message(TARGET_AGENT, message)


# meiva:note
# Report reds (or a tool error) to the operator: the SDK first, the `meiva` CLI as the fallback, and
# a loud stderr line when both fail -- an unreported red is the one outcome this schedule must not
# have. Never fatal: the loop has to keep sweeping.
def _notify(
    checked: int,
    reds: list[tuple[int, str, str]],
    stamp: str,
    *,
    home: Path,
    tool_error: str | None = None,
) -> None:
    log_path = logs_dir(home=home) / LOG_NAME
    if tool_error is not None:
        message = "[sched-dry-import] tool-error (ts=%s): %s\nlog: %s" % (
            stamp,
            tool_error,
            log_path,
        )
    else:
        detail = "\n".join(
            "- id=%s name=%s missing=%s" % (sid, name, missing) for sid, name, missing in reds
        )
        message = "[sched-dry-import] red %s/%s (ts=%s)\n%s\nlog: %s" % (
            len(reds),
            checked,
            stamp,
            detail,
            log_path,
        )
    try:
        meiva.ensure_plugins_loaded()
        _send_sdk(message)
        return
    except Exception as exc:
        print("sdk notify failed: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
    cli = Path(sys.executable).parent / "meiva"
    if cli.is_file():
        try:
            proc = subprocess.run(
                [
                    str(cli),
                    "agents",
                    "send",
                    "--source",
                    "system:sched-dry-import",
                    str(TARGET_AGENT),
                    message,
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if proc.returncode != 0:
                print(
                    "cli notify rc=%s: %s" % (proc.returncode, (proc.stderr or "").strip()[:200]),
                    file=sys.stderr,
                )
            return
        except Exception as exc:
            print("cli notify failed: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
    print("notify failed entirely (ts=%s)" % stamp, file=sys.stderr)


# meiva:note
# One stored script: None when it imports cleanly, else the `missing=` detail that goes on its red
# line. A spawn failure or a timeout is a tool error for that one script, not a reason to abandon
# the sweep.
def check_script(script: str) -> str | None:
    try:
        proc = subprocess.run(
            [sys.executable, str(DRY_IMPORT_CHILD)],
            input=script,
            cwd=str(SOURCE_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=CHILD_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "timeout>%ss" % CHILD_TIMEOUT_S
    except Exception as exc:
        return "tool-error:%s:%s" % (type(exc).__name__, str(exc)[:100])
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    last = lines[-1] if lines else ""
    if last == "CHILD-OK":
        return None
    if last.startswith("CHILD-COMPILE-ERROR:"):
        return "compile-error" + last[len("CHILD-COMPILE-ERROR:") :]
    if last.startswith("CHILD-MODULE:"):
        return last[len("CHILD-MODULE:") :]
    if last.startswith("CHILD-EXC:"):
        return last[len("CHILD-EXC:") :]
    tail = ((proc.stderr or "").strip().splitlines() or [""])[-1][:140]
    return "tool-error:%s" % (tail or "rc=%s" % proc.returncode)


# meiva:note
# Read every row, stopped ones included: a disabled schedule is exactly the one that crashes when
# somebody re-enables it, so it cannot be skipped. The resident entry's inputs are reused when a
# sweep runs inside the schedule; `--once` builds a fresh one.
def _fetch_rows(home: Path) -> list[tuple[int, str, str]]:
    with (
        open_schedule_entry(globals().get("MEIVA_SCHEDULE_INPUTS"), home=home) as inputs,
        inputs.database().connect(autocommit=True) as conn,
        conn.cursor() as cur,
    ):
        cur.execute("SELECT id, name, script FROM schedules ORDER BY id")
        return [(row[0], row[1], row[2] or "") for row in cur.fetchall()]


# meiva:note
# One full sweep. An empty script (a non-Python or cleared row) counts green because there is
# nothing to import; a failure to read the rows at all is the tool-error path (rc=2) and still
# writes a summary line, so a broken sweep never looks like a green one.
def sweep(*, home: Path, notify: bool = True) -> tuple[int, str, list[tuple[int, str, str]]]:
    stamp = datetime.now(ZoneInfo(cluster_timezone())).isoformat(timespec="seconds")
    try:
        rows = _fetch_rows(home)
    except Exception as exc:
        line = "RESULT ts=%s checked=0 green=0 red=0 rc=2" % stamp
        _log([line, "TOOL-ERROR %s: %s" % (type(exc).__name__, str(exc)[:160])], home=home)
        if notify:
            _notify(
                0,
                [],
                stamp,
                home=home,
                tool_error="%s: %s" % (type(exc).__name__, str(exc)[:160]),
            )
        return 2, line, []
    reds: list[tuple[int, str, str]] = []
    checked = 0
    green = 0
    for sid, name, script in rows:
        checked += 1
        if not script.strip():
            green += 1
            continue
        missing = check_script(script)
        if missing is None:
            green += 1
        else:
            reds.append((sid, name, missing))
    rc = 1 if reds else 0
    lines = [
        "RESULT ts=%s checked=%s green=%s red=%s rc=%s" % (stamp, checked, green, len(reds), rc)
    ]
    for sid, name, missing in reds:
        lines.append("RED id=%s name=%s missing=%s" % (sid, name, missing))
    _log(lines, home=home)
    if reds and notify:
        _notify(checked, reds, stamp, home=home)
    return rc, lines[0], reds


def _fire(_slot: datetime, message: str | None, *, home: Path) -> None:
    rc, summary, reds = sweep(home=home, notify=True)
    print("[sched-dry-import] %s (rc=%s) %s" % (message, rc, summary))
    for sid, name, missing in reds:
        print("[sched-dry-import] RED id=%s name=%s missing=%s" % (sid, name, missing))


# meiva:note
# The next-fire line is an observation point, not decoration: `run_loop` never prints when it will
# fire next, this schedule is weekly and silent when green, so without the line there is no way to
# tell "computed correctly and waiting" from "computed wrongly and will never fire".
def main(home: Path) -> None:
    with open_schedule_entry(globals().get("MEIVA_SCHEDULE_INPUTS"), home=home) as inputs:
        print(
            "[sched-dry-import] next fire %s (cron %s)"
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
    if "--check-file" in sys.argv:
        target = Path(sys.argv[sys.argv.index("--check-file") + 1])
        result = check_script(target.read_text(encoding="utf-8"))
        print("CHECK-OK" if result is None else "CHECK-RED missing=%s" % result)
        sys.exit(0 if result is None else 1)
    if "--once" in sys.argv:
        _rc, _summary, _reds = sweep(home=_home, notify="--no-notify" not in sys.argv)
        print(_summary)
        for _sid, _name, _missing in _reds:
            print("RED id=%s name=%s missing=%s" % (_sid, _name, _missing))
        sys.exit(_rc)
    if not os.environ.get("MEIVA_SCHEDULE_ID"):
        print(
            "no MEIVA_SCHEDULE_ID: this file runs as a gateway schedule; use --once for a manual run",
            file=sys.stderr,
        )
        sys.exit(2)
    main(_home)
