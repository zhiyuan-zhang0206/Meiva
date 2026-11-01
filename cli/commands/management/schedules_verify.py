# meiva:note
# `meiva schedules verify [--check-file PATH | --rows-file PATH] [--no-notify]` — the dry-import
# and call-signature sweep.
#
# Read-only one-shot check of every in-store script (stopped rows included), read through the
# gateway, which must be reachable: py_compile, a top-level-imports-only execution in this
# checkout's runner venv, a static read of every name the script uses (any scope) against the names
# it binds, and a `inspect.signature().bind` of every call the script makes into repo code --
# nothing is started, stopped, or written. Catches the drift classes where a repo module move
# (task #4800: #2678, the 2026-09-25 R3 Wave-2 regression), a rename leaving a top-level statement
# reading a name nothing imports (2026-10-01 audit: `base.__file__` after `shared` -> `base`), or a
# changed signature (2026-10-03: `catch_up()` gained a required `db`) leaves a DB-embedded script
# stale and the next (re)start crash-loops.
#
# The line contract is `sweep_outcome`'s: one `RESULT ts=... checked=... green=... red=... rc=...`
# line, one `RED id=... name=... missing=...` per red, `TOOL-ERROR ...` on rc=2; exit codes 0 all
# clean / 1 red / 2 tool error. `--check-file` checks one file off-DB (the falsification hook);
# `--rows-file` sweeps a JSON dump of the table (`[[id, name, script], ...]`) instead of reading it,
# so this checkout never dials the gateway, and never signals (the signal reports the live table);
# otherwise a non-clean sweep emits a `schedule_verify_failed` event unless `--no-notify`.
# meiva:link cli/docs/operator-surfaces/operator-surfaces.meiva.okf.md

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, assert_never

from base import telemetry
from base.cluster.machine import (
    GatewayAccess,
    gateway_api_base,
    gateway_auth_headers,
)
from base.cluster.machine.boot import gateway_access_from_boot
from base.host.net.http_dial import get as dial_get
from base.paths import repo_root
from base.telemetry import EventPipeline
from cli.database.runtime import CommandRuntime

from .child_verdict import child_verdict, spawn_failure_verdict, timeout_verdict
from .sweep_outcome import (
    StoredSchedule,
    SweepOutcome,
    failure_event,
    needs_check,
    summarize,
    unreadable,
)

# meiva:note
# One child's budget. task #3696 exception inventory: a self-imposed guard on a single check child
# -- a healthy top-level import set takes seconds, so a child still running at 90s is wedged and
# must not park the sweep behind it.
_VERIFY_CHILD_TIMEOUT_S = 90.0

_API_TIMEOUT_S = 15.0


# meiva:note
# Where a verify run gets what it checks: one script file off-DB, a JSON dump of the table, or the
# gateway's schedule API.
@dataclass(frozen=True)
class CheckFile:
    path: str


@dataclass(frozen=True)
class RowsFromFile:
    path: str


@dataclass(frozen=True)
class RowsFromGateway:
    access: GatewayAccess


# meiva:note
# The gateway this host would read from could not be resolved (no gateway identity yet, an
# unreadable home `.env`): the failure travels as the source so it takes the same tool-error path as
# an unreadable table -- exit 2, a `TOOL-ERROR` line, a signal -- instead of a traceback.
@dataclass(frozen=True)
class GatewayUnresolved:
    error_type: str
    message: str


SweepSource = CheckFile | RowsFromFile | RowsFromGateway | GatewayUnresolved


# meiva:note
# Dry-import one in-store script, check its name reads and bind its repo-API call sites; None =
# clean, else the RED detail. Runs the check child (`schedule_verify_child`) with this CLI's
# interpreter from the repo root: every sanctioned `meiva` invocation is the checkout's own venv --
# the same `.venv/bin/python` the ScheduleManager launches the runner with.
def check_script(script: str) -> str | None:
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "cli.commands.management.schedule_verify_child"],
            check=False,
            input=script,
            cwd=str(repo_root()),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_VERIFY_CHILD_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return timeout_verdict(_VERIFY_CHILD_TIMEOUT_S)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return spawn_failure_verdict(type(exc).__name__, str(exc))
    return child_verdict(proc.stdout or "", proc.stderr or "", proc.returncode)


# meiva:note
# All schedules as (id, name, script) — stopped rows included, ordered by id.
#
# Read through the gateway: the list route names every row, the per-id route carries the script.
# The sweep needs a reachable gateway; a refused or failed call is the tool-error path. The fleet
# update ships its caller (`schedules_verify_read_rows.py`) to hosts, which finds this function by
# name and reads its parameters: keep both stable across the generations that program supports.
def read_schedule_rows(access: GatewayAccess) -> list[tuple[int, str, str]]:
    headers = gateway_auth_headers(access)
    schedules = f"{gateway_api_base(access.gateway_url)}/api/schedules"
    listing = dial_get(schedules, timeout=_API_TIMEOUT_S, headers=headers)
    listing.raise_for_status()
    rows: list[tuple[int, str, str]] = []
    for summary in sorted(listing.json(), key=lambda row: row["id"]):
        resp = dial_get(f"{schedules}/{summary['id']}", timeout=_API_TIMEOUT_S, headers=headers)
        resp.raise_for_status()
        detail = resp.json()
        rows.append((detail["id"], detail["name"], detail["script"] or ""))
    return rows


def _builtin_templates() -> dict[str, str]:
    from base.daemon.schedules.builtin_schedules import MANIFEST_PATH, load_manifest

    schedules_dir = MANIFEST_PATH.parent
    return {
        entry.name: (schedules_dir / entry.script).read_text(encoding="utf-8")
        for entry in load_manifest(MANIFEST_PATH)
    }


# meiva:note
# The rows `read_schedule_rows` returned, dumped as a JSON list of `[id, name, script]`.
# Built-in schedules (`schedules/manifest.json`) are resynced to this checkout's templates
# at gateway startup (`provision_builtin_schedules`), so an offline pre-update sweep checks
# the template that `up` will install rather than the previous release's stored snapshot;
# non-manifest (agent-created) schedules keep their exact stored script.
def _read_rows_file(path: str) -> list[tuple[int, str, str]]:
    data: Any = json.loads(Path(path).read_text(encoding="utf-8"))
    builtins = _builtin_templates()
    rows: list[tuple[int, str, str]] = []
    for row in data:
        row_id, name, script = int(row[0]), str(row[1]), str(row[2])
        rows.append((row_id, name, builtins.get(name, script)))
    return rows


def _read_rows(source: RowsFromFile | RowsFromGateway) -> tuple[StoredSchedule, ...]:
    match source:
        case RowsFromFile(path):
            rows = _read_rows_file(path)
        case RowsFromGateway(access):
            rows = read_schedule_rows(access)
        case _:
            assert_never(source)
    return tuple(StoredSchedule(*row) for row in rows)


# meiva:note
# Run the dry-import and call-signature sweep over every in-store script and print its lines.
def _sweep(
    source: RowsFromFile | RowsFromGateway | GatewayUnresolved, *, stamp: str
) -> SweepOutcome:
    if isinstance(source, GatewayUnresolved):
        outcome = unreadable(stamp, source.error_type, source.message)
    else:
        # meiva:note
        # Whatever stops the rows from being read is the tool-error path: no failure of a row
        # source may read as a clean sweep.
        try:
            rows = _read_rows(source)
        except Exception as exc:
            outcome = unreadable(stamp, type(exc).__name__, str(exc))
        else:
            results = tuple(
                (row, check_script(row.script) if needs_check(row.script) else None) for row in rows
            )
            outcome = summarize(stamp, results)
    for line in outcome.lines:
        print(line)
    return outcome


# meiva:note
# `--check-file PATH` — dry-import one script file (no gateway, no signal).
def _verify_file(path: str) -> int:
    try:
        source = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"cannot read script file: {exc}", file=sys.stderr)
        return 2
    missing = check_script(source)
    print("CHECK-OK" if missing is None else f"CHECK-RED missing={missing}")
    return 0 if missing is None else 1


def cmd_schedules_verify(
    *,
    source: SweepSource,
    notify: bool,
    now: datetime,
    producer: Callable[[], EventPipeline],
) -> int:
    match source:
        case CheckFile(path):
            return _verify_file(path)
        case RowsFromFile():
            notify = False
            outcome = _sweep(source, stamp=now.isoformat(timespec="seconds"))
        case RowsFromGateway() | GatewayUnresolved():
            outcome = _sweep(source, stamp=now.isoformat(timespec="seconds"))
        case _:
            assert_never(source)
    if notify and outcome.owes_signal:
        report_verify(outcome, producer=producer)
    return outcome.rc


# meiva:note
# Emit one `schedule_verify_failed` event for a non-clean sweep.
#
# The event is the signal a Grafana rule reads; a clean sweep emits nothing, so the alert resolves
# on its own once reds stop recurring.
def report_verify(outcome: SweepOutcome, *, producer: Callable[[], EventPipeline]) -> None:
    event = failure_event(outcome)
    telemetry.emit(
        "telemetry",
        "schedule_verify_failed",
        producer=producer,
        level="error",
        source="schedule-verify",
        attributes={
            "checked": event.checked,
            "red": event.red,
            "tool_error": event.tool_error,
            "detail": event.detail,
        },
    )


# meiva:note
# The instant a report is stamped with: the invocation's own instant, read in the zone its
# configuration names. The zone is the runtime's answer (`CommandRuntime.zone`), asked here instead
# of a second clock built in this command: that clock would carry the UTC instant only, and
# `astimezone(None)` downstream would quietly write the report in the machine's zone.
def stamped_instant(runtime: CommandRuntime) -> datetime:
    return runtime.clock.now().astimezone(runtime.zone)


def h_schedules_verify(args: argparse.Namespace) -> int:
    source: SweepSource
    if args.check_file is not None:
        source = CheckFile(args.check_file)
    elif args.rows_file is not None:
        source = RowsFromFile(args.rows_file)
    else:
        # meiva:note
        # An unresolvable gateway identity is the tool-error path, not a traceback.
        try:
            source = RowsFromGateway(gateway_access_from_boot(args.runtime.config))
        except Exception as exc:
            source = GatewayUnresolved(type(exc).__name__, str(exc))
    stamped = stamped_instant(args.runtime)
    return cmd_schedules_verify(
        source=source,
        notify=not args.no_notify,
        now=stamped,
        producer=args.producer,
    )
