# meiva:note
# Which process is running a schedule, and whether it still is that process.
#
# A schedule's session is an interactive login shell; the runner is a command inside it. The
# session outliving its runner is normal here -- measured 2026-11-04 on company-mini, a runner
# killed with SIGKILL left the shell alive and the session listed, so "a session exists" kept
# answering "the schedule is alive" while nothing was running (#6164).
#
# The runner therefore records itself at start: its pid and the native birth the kernel reports
# for it, and nothing that ages. A reader asks the process table instead of a timestamp, so a
# record cannot decay into a false "alive": it is proved dead, proved alive, or unprovable. A
# session started before this record existed, a record that cannot be parsed, and a process table
# that cannot be read all stay ``UNKNOWN``; only a positive reading of the process table says
# ``DEAD``. That asymmetry is the point -- "I could not read it" must never authorize the
# recovery a death does.
#
# The record is a file under ``<home>/run``, so it dies with the machine rather than with the
# deployment, and a reader outside this process (an operator, a future manager) can read it.

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TypeGuard

import psutil

from base.deploy.schedule_timing import SCHEDULE_LAUNCH_GRACE_S
from base.host.atomic_io import write_text_atomic
from base.host.boundary.untyped import object_of
from base.native_process.ownership import OwnedProcess

__all__ = [
    "RunnerLiveness",
    "RunnerReading",
    "claim_path",
    "clear_claim",
    "read_runner_liveness",
    "write_claim",
]

_log = logging.getLogger(__name__)


class RunnerLiveness(StrEnum):
    # meiva:note
    # the recorded process is in the process table and is the one the record names
    ALIVE = "alive"
    # meiva:note
    # the record is readable and the process it names is gone, is a zombie, or that pid now
    # belongs to a different process
    DEAD = "dead"
    # meiva:note
    # no record, or nothing that could decide: absence of evidence, never permission to act
    UNKNOWN = "unknown"


# meiva:note
# The record as written. ``birth`` is the platform's stable native birth and ``starttime`` the
# Linux start tick, which is the same pair ``OwnedProcess`` compares; it is captured by the
# process itself, so the two readings describe the same process by construction.
@dataclass(frozen=True)
class Claim:
    schedule_id: int
    pid: int
    birth: float
    starttime: int | None

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> Claim | None:
        schedule_id = payload.get("schedule_id")
        pid = payload.get("pid")
        birth = payload.get("birth")
        starttime = payload.get("starttime")
        if not _is_int(schedule_id) or not _is_int(pid):
            return None
        if not isinstance(birth, float) and not _is_int(birth):
            return None
        if starttime is not None and not _is_int(starttime):
            return None
        return cls(schedule_id, pid, float(birth), starttime)


# meiva:note
# A verdict with the sentence that explains it. ``detail`` is what goes in a log line and in the
# stall event, so an operator reads why the manager believed a schedule was down without having
# to reconstruct the process table.
@dataclass(frozen=True)
class RunnerReading:
    schedule_id: int
    liveness: RunnerLiveness
    claim: Claim | None
    detail: str


def claim_path(home: Path, schedule_id: int) -> Path:
    return home / "run" / "schedules" / f"{schedule_id}.runner.json"


# meiva:note
# Recorded once, by the runner, about itself. A record that cannot be written is logged and the
# run continues: a schedule that refuses to start because it cannot describe itself would be a
# worse failure than an unprovable one.
def write_claim(home: Path, schedule_id: int, *, pid: int) -> Path | None:
    try:
        owned = OwnedProcess.capture(psutil.Process(pid))
    except (psutil.Error, RuntimeError) as exc:
        _log.warning("schedule %s could not record its runner identity: %s", schedule_id, exc)
        return None
    path = claim_path(home, schedule_id)
    payload = {
        "schedule_id": schedule_id,
        "pid": owned.pid,
        "birth": owned.birth,
        "starttime": owned.starttime,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # meiva:note
        # No fsync: losing this record in a power cut costs one unprovable answer about a
        # process that died with the same cut.
        write_text_atomic(path, json.dumps(payload, sort_keys=True) + "\n", sync_file=False)
    except OSError as exc:
        _log.warning("schedule %s could not write its runner record: %s", schedule_id, exc)
        return None
    return path


# meiva:note
# Removed by a runner that is exiting on purpose. A record left behind therefore means the process
# did not get to say goodbye -- which is exactly the case a reader has to decide about, and the
# only one. A completed schedule keeps no record; its session may still be listed, and that is
# ``UNKNOWN`` rather than a death.
def clear_claim(home: Path, schedule_id: int) -> None:
    try:
        claim_path(home, schedule_id).unlink(missing_ok=True)
    except OSError as exc:
        _log.warning("schedule %s could not clear its runner record: %s", schedule_id, exc)


# meiva:note
# What the record says about the runner that should own this session. `launched_at` is when that
# session started: a record written before it cannot describe a process from it -- it is the
# previous runner's, left behind because the manager reaped it, and reading it as a death would
# declare a schedule lost a quarter of a second after a successful launch (2026-11-06, schedule 40).
def read_runner_liveness(
    home: Path,
    schedule_id: int,
    *,
    launched_at: float | None = None,
    now: float | None = None,
) -> RunnerReading:
    claim = _read_claim(home, schedule_id)
    if claim is None:
        return RunnerReading(
            schedule_id,
            RunnerLiveness.UNKNOWN,
            None,
            "no runner record (a session started before runners recorded themselves)",
        )
    owned = OwnedProcess(claim.pid, claim.birth, claim.starttime)
    try:
        reading = owned.read_live()
        if owned.judge_live(reading):
            return RunnerReading(
                schedule_id, RunnerLiveness.ALIVE, claim, f"pid {claim.pid} is the recorded process"
            )
    except RuntimeError as exc:
        return RunnerReading(schedule_id, RunnerLiveness.UNKNOWN, claim, str(exc))
    except psutil.Error as exc:
        return RunnerReading(
            schedule_id, RunnerLiveness.UNKNOWN, claim, f"could not read the process table: {exc}"
        )
    if _record_predates_launch(claim, launched_at=launched_at, now=now):
        return RunnerReading(
            schedule_id,
            RunnerLiveness.UNKNOWN,
            claim,
            f"pid {claim.pid} was born before this session started "
            f"({claim.birth:.0f} < {launched_at:.0f}): the previous runner's record",
        )
    if reading is None:
        return RunnerReading(schedule_id, RunnerLiveness.DEAD, claim, f"pid {claim.pid} is gone")
    if reading.status in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
        return RunnerReading(
            schedule_id, RunnerLiveness.DEAD, claim, f"pid {claim.pid} is a zombie"
        )
    return RunnerReading(
        schedule_id,
        RunnerLiveness.DEAD,
        claim,
        f"pid {claim.pid} now belongs to a different process",
    )


# meiva:note
# The one decision this adds: a record born before the session started belongs to the runner this
# launch replaced, and only for as long as a new runner could still be starting up. Past the grace
# it is a death like any other -- an unbounded acceptance would make a session whose runner never
# came up look alive forever.
def _record_predates_launch(claim: Claim, *, launched_at: float | None, now: float | None) -> bool:
    if launched_at is None or now is None or claim.birth >= launched_at:
        return False
    return now - launched_at <= SCHEDULE_LAUNCH_GRACE_S


def _read_claim(home: Path, schedule_id: int) -> Claim | None:
    try:
        payload = object_of(json.loads, claim_path(home, schedule_id).read_text())
    except (OSError, ValueError):
        return None
    if not _is_object_dict(payload):
        return None
    claim = Claim.from_payload(payload)
    if claim is None or claim.schedule_id != schedule_id:
        return None
    return claim


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)
