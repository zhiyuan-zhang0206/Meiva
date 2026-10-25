# meiva:note
# What the window says about the impersonation leases a stop would end, and how long it holds the
# stops open for the operator to release them. A lease list that could not be read is one
# warning, never a refusal: refusing would be the very thing the announcement exists to prevent,
# a window that cannot run.
# meiva:link cli/docs/fleet-update-window/lease-announcement.meiva.okf.md

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TypeGuard


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


@dataclass(frozen=True)
class Lease:
    agent_id: int
    session_id: int
    executor: str
    expires_at: str


@dataclass(frozen=True)
class LeaseReading:
    leases: tuple[Lease, ...]
    unread: str | None


@dataclass(frozen=True)
class CountdownTick:
    left_s: float
    sleep_s: float


def _lease(item: object) -> Lease:
    if not _is_object_dict(item):
        raise TypeError("lease row must be a mapping")
    row = item
    agent_id, session_id = row["agent_id"], row["session_id"]
    executor, expires_at = row["executor"], row["expires_at"]
    if not (isinstance(agent_id, int) and isinstance(session_id, int)):
        raise TypeError("lease ids are integers")
    if not (isinstance(executor, str) and isinstance(expires_at, str)):
        raise TypeError("lease executor and expiry are text")
    return Lease(agent_id, session_id, executor, expires_at)


# meiva:note
# The leases the shipped program printed: its last JSON line. Anything else -- the program's own
# `LEASES failed` line, no JSON at all, a row without the fields the report names -- is an
# unreadable list carrying the program's own words.
def read_leases(output: str) -> LeaseReading:
    lines = output.splitlines()
    try:
        parsed: object = json.loads([ln for ln in lines if ln.startswith("{")][-1])
        if not _is_object_dict(parsed):
            raise TypeError("leases payload must be a mapping")
        listed = parsed["leases"]
        if not _is_object_list(listed):
            raise TypeError("leases list must be a sequence")
        return LeaseReading(tuple(_lease(item) for item in listed), None)
    except (IndexError, KeyError, TypeError, ValueError):
        failed = next((line for line in lines if line.startswith("LEASES failed")), "no output")
        return LeaseReading((), failed)


def unread_warning(detail: str) -> str:
    return (
        f"WARNING: active impersonation leases were not read ({detail}); a takeover this window "
        "ends goes unannounced"
    )


# meiva:note
# The same words under --dry-run and a real run: the stops name the takeovers they would end
# before anything stops.
def announcement(leases: tuple[Lease, ...]) -> tuple[str, ...]:
    if not leases:
        return ("leases: no active impersonation lease",)
    return (
        f"leases: {len(leases)} active impersonation lease(s):",
        *(
            f"leases:   agent {lease.agent_id} session {lease.session_id} "
            f"executor {lease.executor!r} expires {lease.expires_at}"
            for lease in leases
        ),
        "leases: the stop ends every one of them -- a restart never adopts a relay, so the "
        "takeover is aborted mid-task and the native agent resumes: release or renew them first",
    )


# meiva:note
# The countdown that holds the stops open: one line per tick, each tick sleeping the shorter of
# the tick and what is left. 0 seconds is no countdown at all.
def countdown(total_s: int, tick_s: float) -> tuple[CountdownTick, ...]:
    if tick_s <= 0:
        raise ValueError(f"the countdown tick must be positive, not {tick_s}")
    ticks: list[CountdownTick] = []
    left = float(total_s)
    while left > 1e-9:
        step = min(tick_s, left)
        ticks.append(CountdownTick(left, step))
        left -= step
    return tuple(ticks)


def countdown_line(tick: CountdownTick) -> str:
    return f"leases: {tick.left_s:.0f}s left: release or renew them, or let the stop end them"


def dry_run_line(total_s: int) -> str:
    return f"leases: dry run: not waiting {total_s}s"
