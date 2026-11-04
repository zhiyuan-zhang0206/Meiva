# meiva:note
# Bounded best-effort shell-session closure using explicitly known process groups.
#
# Closing a shell session attempts HUP, TERM and KILL, then reports known shell/job
# identities still alive. It never discovers descendants, scans the host process
# table or certifies that every process originally launched from the shell session is
# gone. Residual host processes belong to operational investigation.

from __future__ import annotations

import signal
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import psutil

from base.clock import Timers
from base.host.boundary.fields import (
    int_field,
    optional_float_field,
    optional_list_field,
    str_field,
)
from base.host.boundary.json_values import is_object_dict, is_object_list
from base.native_process.ownership import OwnedProcess
from base.native_process.process_control import ProcessControl
from base.sessions.pty import process_groups
from base.sessions.pty.survivor_role import SurvivorRole


@dataclass(frozen=True)
class Target:
    """One shell to close: its session name and the identity recorded at birth.

    `members` are explicitly recorded job identities. They are cleanup targets,
    not a complete inventory of the session or its descendants.
    """

    name: str
    shell: OwnedProcess
    members: tuple[OwnedProcess, ...] = ()


@dataclass(frozen=True)
class ClosedSession:
    """A busy session whose shell is verified gone.

    `left` are the session's processes that outlived the SIGKILL, as
    (pid, command name); empty when everything of it is gone.
    """

    name: str
    shell: OwnedProcess
    left: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True)
class Survivor:
    """A known cleanup target still alive after the bounded signaling attempt."""

    session: str
    process: OwnedProcess
    role: SurvivorRole


# meiva:note
# Closed busy shell sessions and known survivors; no descendant-clearance proof.
#
# `grace_s` is the grace the closure was asked for and `escalated` says the grace expired with
# live processes, so the SIGKILL leg ran. Both are what happened, reported rather than decided:
# a caller that must judge "did the grace bound this, or did it cost a deadline" reads them
# instead of timing the call.
@dataclass(frozen=True)
class Outcome:
    # meiva:note
    # `grace_s` carries no default: an outcome always knows the grace it ran with, and the clock
    # lattice owns grace literals, not this report.
    grace_s: float
    closed: tuple[ClosedSession, ...] = ()
    survivors: tuple[Survivor, ...] = ()
    escalated: bool = False

    def to_wire(self) -> dict[str, object]:
        return {
            "grace_s": self.grace_s,
            "escalated": self.escalated,
            "closed": [
                {
                    "name": closed.name,
                    "shell": _identity_wire(closed.shell),
                    "left": [list(pair) for pair in closed.left],
                }
                for closed in self.closed
            ],
            "survivors": [
                {
                    "session": survivor.session,
                    "process": _identity_wire(survivor.process),
                    "role": survivor.role.value,
                }
                for survivor in self.survivors
            ],
        }

    @classmethod
    def from_wire(cls, data: Mapping[str, object]) -> Outcome:
        return cls(
            grace_s=_grace_of(data),
            escalated=bool(data.get("escalated", False)),
            closed=tuple(_closed_from_wire(item) for item in _wire_list(data, "closed")),
            survivors=tuple(_survivor_from_wire(item) for item in _wire_list(data, "survivors")),
        )


def _grace_of(data: Mapping[str, object]) -> float:
    if "grace_s" not in data:
        return 0.0
    grace_s = optional_float_field(data, "grace_s")
    if grace_s is None:
        raise TypeError("grace_s is not a number")
    return grace_s


def _identity_wire(identity: OwnedProcess) -> dict[str, object]:
    return {"pid": identity.pid, "birth": identity.birth, "starttime": identity.starttime}


def _wire_list(data: Mapping[str, object], key: str) -> list[object]:
    items = optional_list_field(data, key)
    if items is None:
        raise TypeError(f"{key} is missing or not a list")
    return items


def _wire_object(item: object, label: str) -> dict[str, object]:
    if not is_object_dict(item):
        raise TypeError(f"{label} is not an object")
    return item


def _left_pair(pair: object) -> tuple[int, str]:
    if not is_object_list(pair) or len(pair) != 2:
        raise TypeError("a left entry is not a [pid, name] pair")
    pid, name = pair
    if not isinstance(pid, int) or not isinstance(name, str):
        raise TypeError("a left entry is not a [pid, name] pair")
    return pid, name


def _closed_from_wire(raw: object) -> ClosedSession:
    item = _wire_object(raw, "a closed session")
    return ClosedSession(
        str_field(item, "name"),
        identity_from_wire(_wire_object(item["shell"], "shell")),
        tuple(_left_pair(pair) for pair in _wire_list(item, "left")),
    )


def _survivor_from_wire(raw: object) -> Survivor:
    item = _wire_object(raw, "a survivor")
    return Survivor(
        str_field(item, "session"),
        identity_from_wire(_wire_object(item["process"], "process")),
        SurvivorRole(str_field(item, "role")),
    )


def identity_from_wire(item: Mapping[str, object]) -> OwnedProcess:
    starttime = item["starttime"]
    birth = optional_float_field(item, "birth")
    if birth is None:
        raise TypeError("birth is missing or not a number")
    if starttime is not None and not isinstance(starttime, int):
        raise TypeError("starttime is not an integer")
    return OwnedProcess(int_field(item, "pid"), birth, starttime)


def _known(target: Target) -> tuple[OwnedProcess, ...]:
    return tuple(dict.fromkeys((target.shell, *target.members)))


def _present(identities: Iterable[OwnedProcess]) -> list[OwnedProcess]:
    return [identity for identity in identities if process_groups.live(identity)]


def _named(identities: Iterable[OwnedProcess]) -> tuple[tuple[int, str], ...]:
    names: list[tuple[int, str]] = []
    for identity in identities:
        if not process_groups.live(identity):
            continue
        try:
            name = psutil.Process(identity.pid).name()
        except psutil.NoSuchProcess:
            continue
        if process_groups.live(identity):
            names.append((identity.pid, name))
    return tuple(names)


def close_sessions(
    targets: Iterable[Target],
    *,
    grace_s: float,
    kill_s: float,
    process_control: ProcessControl,
    own_pid: int,
    timers: Timers,
) -> Outcome:
    """Attempt closure of known groups within shared grace and kill budgets.

    A gone/recycled identity is skipped. Concrete signaling/identity errors
    propagate. A known survivor is diagnostic evidence, not a host-wide scan.
    """
    entries = list(targets)
    known = tuple(dict.fromkeys(identity for target in entries for identity in _known(target)))
    process_groups.signal(
        (target.shell for target in entries),
        signal.SIGHUP,
        process_control=process_control,
        own_pid=own_pid,
        timers=timers,
    )
    process_groups.signal(
        (member for target in entries for member in target.members if member != target.shell),
        signal.SIGTERM,
        process_control=process_control,
        own_pid=own_pid,
        timers=timers,
    )
    remaining = process_groups.wait(known, grace_s, timers=timers)
    escalated = bool(remaining)
    if remaining:
        process_groups.signal(
            remaining,
            signal.SIGKILL,
            process_control=process_control,
            own_pid=own_pid,
            timers=timers,
        )
        process_groups.wait(remaining, kill_s, timers=timers)
    survivors: list[Survivor] = []
    closed: list[ClosedSession] = []
    for target in entries:
        left = _present(_known(target))
        survivors.extend(
            Survivor(
                target.name,
                identity,
                SurvivorRole.SHELL if identity == target.shell else SurvivorRole.JOB,
            )
            for identity in left
        )
        if target.members and not process_groups.live(target.shell):
            closed.append(ClosedSession(target.name, target.shell, _named(left)))
    return Outcome(
        grace_s,
        closed=tuple(closed),
        survivors=tuple(survivors),
        escalated=escalated,
    )
