# meiva:note
# What one round of shell reclamation does, decided from values.
#
# `plan_round` groups the expired rows by home machine and sets aside the rows that have no usable
# machine. `settle` reads a runner's `shell_kill` answer into the verdict the row is settled with,
# or says the answer is malformed and the row must wait. `reclaim_notice` is the interruption
# message the owner reads. The shell dials the machines, deletes the rows and sends the messages;
# nothing here reads a clock, a registry or a connection.
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, assert_never
from zoneinfo import ZoneInfo

from base.api_contracts.agent_contract import ShellKillMode
from services.upkeep.ttl_reaper.owner_text import human_ttl, wall_clock

# meiva:note
# The home machine a row carries when its owner's machine could not be resolved.
UNKNOWN_MACHINE = "unknown"


@dataclass(frozen=True)
class ExpiredShell:
    agent_id: int
    session_id: int
    machine: str | None
    expires_at: datetime
    created_at: datetime


@dataclass(frozen=True)
class MachineBatch:
    machine: str
    shells: tuple[ExpiredShell, ...]


@dataclass(frozen=True)
class RoundPlan:
    batches: tuple[MachineBatch, ...]
    unplaced: tuple[ExpiredShell, ...]


# meiva:note
# The verdict a row is settled with. `mode` is None for a machine that is absent from the
# registry: that verdict is local evidence, never something a runner's answer may claim.
@dataclass(frozen=True)
class Settlement:
    mode: ShellKillMode | None
    interrupted: bool
    name: str | None

    @property
    def verdict(self) -> Literal["killed", "absent", "machine_absent"]:
        match self.mode:
            case None:
                return "machine_absent"
            case ShellKillMode.KILLED:
                return "killed"
            case ShellKillMode.ABSENT:
                return "absent"
            case _:
                assert_never(self.mode)


# meiva:note
# Rows of one machine stay in the order they arrived; machines keep first-seen order. A row whose
# machine is missing or unknown is returned in `unplaced` and waits for the next round.
def plan_round(rows: Sequence[ExpiredShell]) -> RoundPlan:
    placed: dict[str, list[ExpiredShell]] = {}
    unplaced: list[ExpiredShell] = []
    for shell in rows:
        if not shell.machine or shell.machine == UNKNOWN_MACHINE:
            unplaced.append(shell)
            continue
        placed.setdefault(shell.machine, []).append(shell)
    return RoundPlan(
        batches=tuple(MachineBatch(machine, tuple(shells)) for machine, shells in placed.items()),
        unplaced=tuple(unplaced),
    )


# meiva:note
# The settlement of a machine the registry does not know: the row is dropped silently, since
# nothing on a gone machine was interrupted.
def machine_absent_settlement() -> Settlement:
    return Settlement(mode=None, interrupted=False, name=None)


# meiva:note
# A runner's `shell_kill` answer as a settlement, or None when it is malformed (no mode, an
# unknown mode, or a kill that does not say whether it interrupted a job). Only a kill that
# interrupted a running job is notified; an absent session never is.
def settle(answer: Mapping[str, object]) -> Settlement | None:
    try:
        mode = ShellKillMode(answer["mode"])
        interrupted = mode is ShellKillMode.KILLED and bool(answer["interrupted"])
    except (KeyError, ValueError):
        return None
    name = answer.get("name")
    return Settlement(
        mode=mode, interrupted=interrupted, name=name if isinstance(name, str) else None
    )


# meiva:note
# The message that tells an owner its shell was reclaimed while a job ran, or None when the reap
# interrupted nothing. It states when the TTL expired and how long it was.
def reclaim_notice(
    shell: ExpiredShell, settlement: Settlement, *, zone: ZoneInfo | None, today: date
) -> str | None:
    if not settlement.interrupted:
        return None
    label = (
        f"Shell session {settlement.name!r} (id {shell.session_id}, agent {shell.agent_id})"
        if settlement.name
        else f"Shell session {shell.session_id} (agent {shell.agent_id})"
    )
    ttl = human_ttl((shell.expires_at - shell.created_at).total_seconds())
    stamp = wall_clock(shell.expires_at, zone=zone, today=today)
    return (
        f"{label} was reclaimed after its TTL expired at {stamp}, TTL {ttl}, "
        "interrupting a running task."
    )
