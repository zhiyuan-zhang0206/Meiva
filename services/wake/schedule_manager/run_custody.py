# meiva:note
# Durable custody of one declarative schedule run and its dedicated process group.

from __future__ import annotations

import json
import math
import signal
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TypeGuard

import psutil

from base.host.atomic_io import write_text_atomic
from base.host.boundary.untyped import object_of
from base.native_process.ownership import LiveReading, OwnedProcess
from base.native_process.process_control import ProcessControl


class ClaimReadState(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    UNREADABLE = "unreadable"


class LeaderLiveness(StrEnum):
    ALIVE = "alive"
    DEAD = "dead"
    FOREIGN = "foreign"
    UNKNOWN = "unknown"


class GroupLiveness(StrEnum):
    PRESENT = "present"
    EMPTY = "empty"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RunClaim:
    schedule_id: int
    run_id: int
    leader: OwnedProcess
    pgid: int
    deadline: datetime

    def __post_init__(self) -> None:
        if self.schedule_id < 1 or self.run_id < 1:
            raise ValueError("schedule and run ids must be positive")
        if self.pgid != self.leader.pid:
            raise ValueError("a declarative run must own a dedicated leader process group")
        if self.deadline.tzinfo is None:
            raise ValueError("a declarative run deadline must be timezone-aware")


@dataclass(frozen=True)
class ClaimReading:
    state: ClaimReadState
    claim: RunClaim | None
    detail: str


@dataclass(frozen=True)
class GroupObservation:
    leader: LeaderLiveness
    group: GroupLiveness
    detail: str


def claim_path(work_dir: Path, run_id: int) -> Path:
    return work_dir / f".run-{run_id}.claim.json"


# meiva:note
# Capture only after the child owns the dedicated group its durable claim will name.
def capture_run_leader(pid: int, *, process_control: ProcessControl) -> OwnedProcess:
    leader = OwnedProcess.capture(psutil.Process(pid))
    pgid = process_control.getpgid(pid)
    if pgid != pid:
        raise RuntimeError(f"declarative run leader {pid} does not lead its process group")
    if not leader.live():
        raise psutil.NoSuchProcess(pid, msg="declarative run leader exited during capture")
    return leader


def write_run_claim(work_dir: Path, claim: RunClaim) -> Path:
    path = claim_path(work_dir, claim.run_id)
    payload = {
        "schedule_id": claim.schedule_id,
        "run_id": claim.run_id,
        "pid": claim.leader.pid,
        "birth": claim.leader.birth,
        "starttime": claim.leader.starttime,
        "pgid": claim.pgid,
        "deadline_epoch": claim.deadline.timestamp(),
    }
    write_text_atomic(path, json.dumps(payload, sort_keys=True) + "\n", sync_file=False)
    return path


def read_run_claim(work_dir: Path, schedule_id: int, run_id: int) -> ClaimReading:
    path = claim_path(work_dir, run_id)
    try:
        raw = object_of(json.loads, path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ClaimReading(ClaimReadState.ABSENT, None, "no run claim")
    except (OSError, ValueError) as exc:
        return ClaimReading(ClaimReadState.UNREADABLE, None, f"run claim unreadable: {exc}")
    if not _is_dict(raw):
        return ClaimReading(ClaimReadState.UNREADABLE, None, "run claim is not an object")
    try:
        claim = _claim_from_payload(raw)
    except (OverflowError, TypeError, ValueError) as exc:
        return ClaimReading(ClaimReadState.UNREADABLE, None, f"invalid run claim: {exc}")
    if claim.schedule_id != schedule_id or claim.run_id != run_id:
        return ClaimReading(
            ClaimReadState.UNREADABLE,
            None,
            "run claim identity does not match its schedule run",
        )
    return ClaimReading(ClaimReadState.PRESENT, claim, "run claim loaded")


def clear_run_claim(work_dir: Path, run_id: int) -> None:
    claim_path(work_dir, run_id).unlink(missing_ok=True)


def observe_run_group(claim: RunClaim, *, process_control: ProcessControl) -> GroupObservation:
    leader, detail = _leader_liveness(claim.leader)
    if leader is LeaderLiveness.UNKNOWN:
        return GroupObservation(leader, GroupLiveness.UNKNOWN, detail)
    if leader is LeaderLiveness.FOREIGN:
        # meiva:note
        # POSIX reserves a numeric PGID while any member remains. Reuse of the leader PID
        # therefore proves the original group emptied before the foreign birth appeared.
        return GroupObservation(leader, GroupLiveness.EMPTY, detail)
    if leader is LeaderLiveness.ALIVE:
        try:
            pgid = process_control.getpgid(claim.leader.pid)
            second, second_detail = _leader_liveness(claim.leader)
        except (OSError, psutil.Error, RuntimeError) as exc:
            return GroupObservation(LeaderLiveness.UNKNOWN, GroupLiveness.UNKNOWN, str(exc))
        if second is LeaderLiveness.ALIVE and pgid == claim.pgid:
            return GroupObservation(second, GroupLiveness.PRESENT, second_detail)
        if second is LeaderLiveness.UNKNOWN or pgid != claim.pgid:
            return GroupObservation(
                LeaderLiveness.UNKNOWN,
                GroupLiveness.UNKNOWN,
                f"run leader group identity changed ({pgid} != {claim.pgid})",
            )
        leader, detail = second, second_detail
        if leader is LeaderLiveness.FOREIGN:
            return GroupObservation(leader, GroupLiveness.EMPTY, detail)
    group, group_detail = _probe_group(claim.pgid, process_control=process_control)
    return GroupObservation(leader, group, f"{detail}; {group_detail}")


# meiva:note
# SIGKILL the claimed group only while its original numeric custody is provable.
def kill_run_group(claim: RunClaim, *, process_control: ProcessControl) -> GroupObservation:
    observed = observe_run_group(claim, process_control=process_control)
    if observed.group is not GroupLiveness.PRESENT:
        return observed
    # meiva:note
    # Recheck PID reuse immediately before the group signal. If the leader is gone, any
    # surviving original member keeps its numeric PGID reserved during this check.
    leader, detail = _leader_liveness(claim.leader)
    if leader is LeaderLiveness.UNKNOWN:
        return GroupObservation(leader, GroupLiveness.UNKNOWN, detail)
    if leader is LeaderLiveness.FOREIGN:
        return GroupObservation(leader, GroupLiveness.EMPTY, detail)
    if leader is LeaderLiveness.ALIVE:
        try:
            pgid = process_control.getpgid(claim.leader.pid)
            confirmed, confirmed_detail = _leader_liveness(claim.leader)
            if confirmed is LeaderLiveness.FOREIGN:
                return GroupObservation(confirmed, GroupLiveness.EMPTY, confirmed_detail)
            if confirmed is not LeaderLiveness.ALIVE or pgid != claim.pgid:
                return GroupObservation(
                    LeaderLiveness.UNKNOWN,
                    GroupLiveness.UNKNOWN,
                    "run leader no longer owns its claimed process group",
                )
        except OSError as exc:
            return GroupObservation(LeaderLiveness.UNKNOWN, GroupLiveness.UNKNOWN, str(exc))
    try:
        process_control.killpg(claim.pgid, signal.SIGKILL)
    except ProcessLookupError:
        return GroupObservation(leader, GroupLiveness.EMPTY, "run process group is empty")
    except OSError as exc:
        return GroupObservation(leader, GroupLiveness.UNKNOWN, f"group kill failed: {exc}")
    return observe_run_group(claim, process_control=process_control)


def _leader_liveness(identity: OwnedProcess) -> tuple[LeaderLiveness, str]:
    try:
        reading = identity.read_live()
        if reading is None:
            return LeaderLiveness.DEAD, f"run leader {identity.pid} is gone"
        if identity.judge_live(reading):
            return LeaderLiveness.ALIVE, f"run leader {identity.pid} is alive"
        if _same_birth(identity, reading):
            return LeaderLiveness.DEAD, f"run leader {identity.pid} is dead"
        return LeaderLiveness.FOREIGN, f"pid {identity.pid} belongs to a different process"
    except (OSError, psutil.Error, RuntimeError, ValueError) as exc:
        return LeaderLiveness.UNKNOWN, f"run leader identity is unprovable: {exc}"


def _same_birth(identity: OwnedProcess, reading: LiveReading) -> bool:
    if identity.starttime is not None:
        if reading.ticks is None:
            if reading.pid_exists:
                raise RuntimeError(f"cannot verify process identity for PID {identity.pid}")
            return False
        return reading.ticks == identity.starttime
    return reading.identity is not None and identity.same_birth(reading.identity)


def _probe_group(pgid: int, *, process_control: ProcessControl) -> tuple[GroupLiveness, str]:
    try:
        process_control.killpg(pgid, 0)
    except ProcessLookupError:
        return GroupLiveness.EMPTY, f"run process group {pgid} is empty"
    except OSError as exc:
        return GroupLiveness.UNKNOWN, f"run process group {pgid} is unprovable: {exc}"
    return GroupLiveness.PRESENT, f"run process group {pgid} still exists"


def _claim_from_payload(raw: dict[str, object]) -> RunClaim:
    schedule_id = raw.get("schedule_id")
    run_id = raw.get("run_id")
    pid = raw.get("pid")
    birth = raw.get("birth")
    starttime = raw.get("starttime")
    pgid = raw.get("pgid")
    deadline_epoch = raw.get("deadline_epoch")
    if not (_is_int(schedule_id) and _is_int(run_id) and _is_int(pid) and _is_int(pgid)):
        raise ValueError("schedule_id, run_id, pid, and pgid must be integers")
    if not _is_number(birth) or not _is_number(deadline_epoch):
        raise ValueError("birth and deadline_epoch must be finite numbers")
    if starttime is not None and not _is_int(starttime):
        raise ValueError("starttime must be an integer or null")
    if not math.isfinite(float(birth)) or not math.isfinite(float(deadline_epoch)):
        raise ValueError("birth and deadline_epoch must be finite numbers")
    return RunClaim(
        schedule_id=schedule_id,
        run_id=run_id,
        leader=OwnedProcess(pid, float(birth), starttime),
        pgid=pgid,
        deadline=datetime.fromtimestamp(float(deadline_epoch), UTC),
    )


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> TypeGuard[int | float]:
    return (isinstance(value, int) and not isinstance(value, bool)) or isinstance(value, float)


def _is_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)
