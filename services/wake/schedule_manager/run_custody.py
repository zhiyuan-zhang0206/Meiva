# meiva:note
# Durable custody of one declarative schedule run and its dedicated process group.

from __future__ import annotations

import json
import math
import signal
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol, TypeGuard

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


class ProcessIdentityControl(Protocol):
    def read_live(self, identity: OwnedProcess) -> LiveReading | None: ...

    def signal_pinned(self, identity: OwnedProcess, signum: int) -> bool: ...


class _SystemProcessIdentityControl:
    def read_live(self, identity: OwnedProcess) -> LiveReading | None:
        return identity.read_live()

    def signal_pinned(self, identity: OwnedProcess, signum: int) -> bool:
        return identity.signal_pinned(signum)


SYSTEM_PROCESS_IDENTITY_CONTROL: ProcessIdentityControl = _SystemProcessIdentityControl()


@dataclass(frozen=True)
class RunClaim:
    schedule_id: int
    run_id: int
    leader: OwnedProcess
    pgid: int
    deadline: datetime
    member: OwnedProcess | None = None

    def __post_init__(self) -> None:
        if self.schedule_id < 1 or self.run_id < 1:
            raise ValueError("schedule and run ids must be positive")
        if self.pgid != self.leader.pid:
            raise ValueError("a declarative run must own a dedicated leader process group")
        if self.member is not None and self.member.pid == self.leader.pid:
            raise ValueError("a declarative run member must differ from its group leader")
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
        "member": (
            None
            if claim.member is None
            else {
                "pid": claim.member.pid,
                "birth": claim.member.birth,
                "starttime": claim.member.starttime,
            }
        ),
    }
    write_text_atomic(path, json.dumps(payload, sort_keys=True) + "\n", sync_file=False)
    return path


# meiva:note
# The payload waits on its own gate until this native identity is in the durable claim.
def register_run_member(
    work_dir: Path,
    schedule_id: int,
    run_id: int,
    pid: int,
    *,
    process_control: ProcessControl,
) -> OwnedProcess:
    reading = read_run_claim(work_dir, schedule_id, run_id)
    if reading.state is not ClaimReadState.PRESENT or reading.claim is None:
        raise RuntimeError(f"cannot register payload for run {run_id}: {reading.detail}")
    member = OwnedProcess.capture(psutil.Process(pid))
    pgid = process_control.getpgid(pid)
    if pgid != reading.claim.pgid:
        raise RuntimeError(
            f"payload {pid} joined process group {pgid}, expected {reading.claim.pgid}"
        )
    if not member.live():
        raise psutil.NoSuchProcess(pid, msg="declarative payload exited during capture")
    write_run_claim(work_dir, replace(reading.claim, member=member))
    return member


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


def observe_run_group(
    claim: RunClaim,
    *,
    process_control: ProcessControl,
    identity_control: ProcessIdentityControl,
) -> GroupObservation:
    leader, leader_is_anchor, detail = _identity_group_anchor(
        claim.leader,
        claim.pgid,
        process_control=process_control,
        identity_control=identity_control,
    )
    if leader is LeaderLiveness.UNKNOWN:
        return GroupObservation(leader, GroupLiveness.UNKNOWN, detail)
    if leader is LeaderLiveness.FOREIGN:
        # meiva:note
        # POSIX reserves a numeric PGID while any member remains. Reuse of the leader PID
        # therefore proves the original group emptied before the foreign birth appeared.
        return GroupObservation(leader, GroupLiveness.EMPTY, detail)
    if leader_is_anchor:
        return GroupObservation(leader, GroupLiveness.PRESENT, detail)
    member_detail = "no captured payload member"
    if claim.member is not None:
        member, member_is_anchor, member_detail = _identity_group_anchor(
            claim.member,
            claim.pgid,
            process_control=process_control,
            identity_control=identity_control,
        )
        if member is LeaderLiveness.UNKNOWN:
            return GroupObservation(leader, GroupLiveness.UNKNOWN, member_detail)
        if member_is_anchor:
            return GroupObservation(leader, GroupLiveness.PRESENT, member_detail)
    group, group_detail = _probe_group(claim.pgid, process_control=process_control)
    if group is GroupLiveness.PRESENT:
        group = GroupLiveness.UNKNOWN
        group_detail = "numeric group exists without a live captured run member"
    return GroupObservation(leader, group, f"{detail}; {member_detail}; {group_detail}")


# meiva:note
# SIGKILL the claimed group only while its original numeric custody is provable.
def kill_run_group(
    claim: RunClaim,
    *,
    process_control: ProcessControl,
    identity_control: ProcessIdentityControl,
) -> GroupObservation:
    observed = observe_run_group(
        claim,
        process_control=process_control,
        identity_control=identity_control,
    )
    if observed.group is not GroupLiveness.PRESENT:
        return observed
    # meiva:note
    # Stop one exact captured birth before signalling the numeric group. The stopped member
    # reserves its PGID across the final check and kill, so another generation cannot take it.
    anchor, detail = _live_claim_anchor(
        claim,
        process_control=process_control,
        identity_control=identity_control,
    )
    if anchor is None:
        return GroupObservation(observed.leader, GroupLiveness.UNKNOWN, detail)
    try:
        pinned = identity_control.signal_pinned(anchor, signal.SIGSTOP)
    except (OSError, psutil.Error, RuntimeError, ValueError) as exc:
        return GroupObservation(
            observed.leader, GroupLiveness.UNKNOWN, f"run member pin failed: {exc}"
        )
    if not pinned:
        return GroupObservation(
            observed.leader, GroupLiveness.UNKNOWN, "captured run member exited before pin"
        )
    state, still_anchor, detail = _identity_group_anchor(
        anchor,
        claim.pgid,
        process_control=process_control,
        identity_control=identity_control,
    )
    if state is not LeaderLiveness.ALIVE or not still_anchor:
        return GroupObservation(observed.leader, GroupLiveness.UNKNOWN, detail)
    try:
        process_control.killpg(claim.pgid, signal.SIGKILL)
    except ProcessLookupError:
        return GroupObservation(observed.leader, GroupLiveness.EMPTY, "run process group is empty")
    except OSError as exc:
        try:
            resumed = identity_control.signal_pinned(anchor, signal.SIGCONT)
        except (OSError, psutil.Error, RuntimeError, ValueError) as resume_exc:
            return GroupObservation(
                observed.leader,
                GroupLiveness.UNKNOWN,
                f"group kill failed: {exc}; run member resume failed: {resume_exc}",
            )
        detail = f"group kill failed: {exc}"
        if not resumed:
            detail += "; captured run member exited before resume"
        return GroupObservation(observed.leader, GroupLiveness.UNKNOWN, detail)
    return observe_run_group(
        claim,
        process_control=process_control,
        identity_control=identity_control,
    )


def _leader_liveness(
    identity: OwnedProcess, *, identity_control: ProcessIdentityControl
) -> tuple[LeaderLiveness, str]:
    try:
        reading = identity_control.read_live(identity)
        if reading is None:
            return LeaderLiveness.DEAD, f"captured process {identity.pid} is gone"
        if identity.judge_live(reading):
            return LeaderLiveness.ALIVE, f"captured process {identity.pid} is alive"
        if _same_birth(identity, reading):
            return LeaderLiveness.DEAD, f"captured process {identity.pid} is dead"
        return LeaderLiveness.FOREIGN, f"pid {identity.pid} belongs to a different process"
    except (OSError, psutil.Error, RuntimeError, ValueError) as exc:
        return LeaderLiveness.UNKNOWN, f"captured process identity is unprovable: {exc}"


def _identity_group_anchor(
    identity: OwnedProcess,
    pgid: int,
    *,
    process_control: ProcessControl,
    identity_control: ProcessIdentityControl,
) -> tuple[LeaderLiveness, bool, str]:
    state, detail = _leader_liveness(identity, identity_control=identity_control)
    if state is not LeaderLiveness.ALIVE:
        return state, False, detail
    try:
        observed_pgid = process_control.getpgid(identity.pid)
        confirmed, confirmed_detail = _leader_liveness(identity, identity_control=identity_control)
    except (OSError, psutil.Error, RuntimeError, ValueError) as exc:
        return LeaderLiveness.UNKNOWN, False, f"run member group is unprovable: {exc}"
    if confirmed is not LeaderLiveness.ALIVE:
        return confirmed, False, confirmed_detail
    if observed_pgid != pgid:
        return (
            LeaderLiveness.UNKNOWN,
            False,
            f"captured run member moved to process group {observed_pgid}, expected {pgid}",
        )
    return confirmed, True, confirmed_detail


def _live_claim_anchor(
    claim: RunClaim,
    *,
    process_control: ProcessControl,
    identity_control: ProcessIdentityControl,
) -> tuple[OwnedProcess | None, str]:
    for identity in (claim.leader, claim.member):
        if identity is None:
            continue
        state, is_anchor, detail = _identity_group_anchor(
            identity,
            claim.pgid,
            process_control=process_control,
            identity_control=identity_control,
        )
        if state is LeaderLiveness.UNKNOWN:
            return None, detail
        if is_anchor:
            return identity, detail
    return None, "no live captured run member owns the claimed process group"


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
    member_raw = raw.get("member")
    if not (_is_int(schedule_id) and _is_int(run_id) and _is_int(pid) and _is_int(pgid)):
        raise ValueError("schedule_id, run_id, pid, and pgid must be integers")
    if not _is_number(birth) or not _is_number(deadline_epoch):
        raise ValueError("birth and deadline_epoch must be finite numbers")
    if starttime is not None and not _is_int(starttime):
        raise ValueError("starttime must be an integer or null")
    if not math.isfinite(float(birth)) or not math.isfinite(float(deadline_epoch)):
        raise ValueError("birth and deadline_epoch must be finite numbers")
    member = _member_from_payload(member_raw)
    return RunClaim(
        schedule_id=schedule_id,
        run_id=run_id,
        leader=OwnedProcess(pid, float(birth), starttime),
        pgid=pgid,
        deadline=datetime.fromtimestamp(float(deadline_epoch), UTC),
        member=member,
    )


def _member_from_payload(raw: object) -> OwnedProcess | None:
    if raw is None:
        return None
    if not _is_dict(raw):
        raise ValueError("member must be an object or null")
    pid = raw.get("pid")
    birth = raw.get("birth")
    starttime = raw.get("starttime")
    if not _is_int(pid) or not _is_number(birth):
        raise ValueError("member pid and birth must be numbers")
    if starttime is not None and not _is_int(starttime):
        raise ValueError("member starttime must be an integer or null")
    if not math.isfinite(float(birth)):
        raise ValueError("member birth must be finite")
    return OwnedProcess(pid, float(birth), starttime)


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> TypeGuard[int | float]:
    return (isinstance(value, int) and not isinstance(value, bool)) or isinstance(value, float)


def _is_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)
