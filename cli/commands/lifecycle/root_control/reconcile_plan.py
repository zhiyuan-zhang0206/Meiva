# meiva:note
# What to resume, refuse, report or stop once the root's tree has been read.
#
# Every function takes the typed snapshot (and the request) and returns a plan; the shell
# in `root_driver` prints it, calls the root and raises. Unit ids stay bare here — the
# shell composes the display names.

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TypeGuard

from base.native_process.root_control.ipc import UnitState
from cli.commands.lifecycle.root_control.snapshot import RootSnapshot, UnitRow, root_units

__all__ = [
    "LeaveTree",
    "ResumePlan",
    "StopUnits",
    "TreeStopPlan",
    "UnitProblem",
    "UnitResume",
    "changed_inputs",
    "classify_launch",
    "generation_refusal",
    "plan_resume",
    "plan_tree_stop",
]

# meiva:note
# A unit's `intent` shares the vocabulary of its `state`: the supervisor's policy fact is
# "running" or "stopped", the process state it is measured against.
_RUNNING = UnitState.RUNNING.value
_ROOT_LAUNCH_INPUTS = "root launch inputs"


# meiva:note
# A unit to bring up, and what the root last said about it (the reason it is down).
@dataclass(frozen=True)
class UnitResume:
    unit_id: str
    reason: str


# meiva:note
# `up` in roster order; `held_down` are units an open restart breaker keeps down.
@dataclass(frozen=True)
class ResumePlan:
    up: tuple[UnitResume, ...]
    held_down: tuple[str, ...]


# meiva:note
# A requested unit that should run but is not in a position to.
@dataclass(frozen=True)
class UnitProblem:
    unit_id: str
    detail: str


# meiva:note
# Every unit is preserved, so nothing is stopped and the root stays up.
@dataclass(frozen=True)
class LeaveTree: ...


# meiva:note
# Take `unit_ids` down wave by wave; `stop_root` when none is kept, because the root has
# nothing left to host. `waves` is `None` when the snapshot carries no `stop_tier` (an older
# root or untiered list), in which case `stop_waves` falls back to one unit per wave.
@dataclass(frozen=True)
class StopUnits:
    unit_ids: tuple[str, ...]
    stop_root: bool
    waves: tuple[tuple[str, ...], ...] | None = None

    @property
    def stop_waves(self) -> tuple[tuple[str, ...], ...]:
        return (
            self.waves if self.waves is not None else tuple((unit_id,) for unit_id in self.unit_ids)
        )


TreeStopPlan = LeaveTree | StopUnits


# meiva:note
# Resume stopped units within the same immutable root generation.
#
# An explicit operator start also resumes a unit whose recorded intent is stopped:
# `intent` is the supervisor's policy fact and `state` its observed process state. The
# OS boot job's retried start is not an operator: with `honor_restart_budget` it leaves
# a unit down whose circuit breaker is open, because the health monitor keeps retrying
# that unit on its own backoff ladder and the boot job must not race it.
def plan_resume(
    requested: Sequence[str], snapshot: RootSnapshot, *, honor_restart_budget: bool
) -> ResumePlan:
    units = root_units(snapshot)
    health = snapshot.get("health", {})
    up: list[UnitResume] = []
    held_down: list[str] = []
    for unit_id in requested:
        unit = units.get(unit_id)
        if unit is None:
            raise ValueError(f"the root runs no unit {unit_id!r}")
        budget = health.get(unit_id)
        if honor_restart_budget and budget is not None and budget["restart_limit_reached"]:
            held_down.append(unit_id)
        elif unit["state"] != _RUNNING or unit.get("intent") != _RUNNING:
            up.append(
                UnitResume(
                    unit_id, unit.get("last_error") or unit.get("last_exit") or unit["state"]
                )
            )
    return ResumePlan(tuple(up), tuple(held_down))


# meiva:note
# The launch verdict: units that should run but could not be spawned.
def classify_launch(requested: Sequence[str], snapshot: RootSnapshot) -> tuple[UnitProblem, ...]:
    units = root_units(snapshot)
    problems: list[UnitProblem] = []
    for unit_id in requested:
        unit = units.get(unit_id)
        if unit is None:
            problems.append(UnitProblem(unit_id, "missing from the meiva-root tree"))
            continue
        error = unit.get("last_error")
        if unit["state"] != _RUNNING and error:
            problems.append(UnitProblem(unit_id, error))
    return tuple(problems)


# meiva:note
# What differs between a running root and the requested launch, sorted.
#
# A running root may be reused only for the exact requested launch inputs: every
# requested unit must carry the requested manifest digest (`desired`, by unit id),
# the root must serve the requested launch digest and, when the request reconciles the
# whole tree, it must run no unit beyond the requested ones.
def changed_inputs(
    desired: Mapping[str, str],
    launch_digest: str | None,
    snapshot: RootSnapshot,
    *,
    reconcile: bool,
) -> tuple[str, ...]:
    units = root_units(snapshot)
    changed = {
        unit_id
        for unit_id, digest in desired.items()
        if (live := units.get(unit_id)) is None or live.get("manifest_digest") != digest
    }
    if snapshot["root"]["launch_digest"] != launch_digest:
        changed.add(_ROOT_LAUNCH_INPUTS)
    if reconcile:
        changed |= units.keys() - desired.keys()
    return tuple(sorted(changed))


# meiva:note
# The refusal to reuse a root whose generation `changed_inputs` found different.
def generation_refusal(changed: Iterable[str]) -> str:
    return (
        "requested services change the immutable root generation: "
        + ", ".join(sorted(changed))
        + "; run meiva stop before starting with changed services or configuration"
    )


def _subtree_stop_waves(
    parent_id: str,
    children_of: Mapping[str, tuple[str, ...]],
    tiers: Mapping[str, int],
) -> list[tuple[str, ...]]:
    kids = children_of.get(parent_id, ())
    if not kids:
        return []
    by_tier: dict[int, list[str]] = {}
    for kid in kids:
        by_tier.setdefault(tiers[kid], []).append(kid)
    waves: list[tuple[str, ...]] = []
    for tier in sorted(by_tier):
        subtrees = [
            [*_subtree_stop_waves(kid, children_of, tiers), (kid,)] for kid in by_tier[tier]
        ]
        depth = max(len(sub) for sub in subtrees)
        padded = [[()] * (depth - len(sub)) + sub for sub in subtrees]
        for level in range(depth):
            merged = tuple(unit_id for sub in padded for unit_id in sub[level])
            if merged:
                waves.append(merged)
    return waves


def _is_unit_map(
    units: Mapping[str, UnitRow] | Iterable[str],
) -> TypeGuard[Mapping[str, UnitRow]]:
    return isinstance(units, Mapping)


def _plan_tiered_waves(
    units: Mapping[str, UnitRow], stopping_ids: tuple[str, ...]
) -> tuple[tuple[str, ...], ...] | None:
    stopping_set = frozenset(stopping_ids)
    children: dict[str, list[str]] = {}
    tiers: dict[str, int] = {}
    for unit_id in stopping_ids:
        tier = units[unit_id].get("stop_tier")
        if tier is None:
            return None
        tiers[unit_id] = tier
        cursor = units[unit_id].get("attach", "root")
        seen = {unit_id}
        while cursor != "root" and cursor in units and cursor not in stopping_set:
            if cursor in seen:
                cursor = "root"
                break
            seen.add(cursor)
            cursor = units[cursor].get("attach", "root")
        parent = cursor if cursor in stopping_set else "root"
        children.setdefault(parent, []).append(unit_id)
    frozen_children = {parent: tuple(kids) for parent, kids in children.items()}
    return tuple(_subtree_stop_waves("root", frozen_children, tiers))


# meiva:note
# Stop everything, or only the units not preserved.
#
# `preserve` holds bare unit ids. Stopping every unit also stops the root itself;
# keeping at least one unit leaves the root running to host it. When `units` is a
# snapshot mapping whose stopping members report `stop_tier`, `StopUnits.waves`
# groups independent units in the same tier so the shell stops each wave in parallel.
def plan_tree_stop(
    units: Mapping[str, UnitRow] | Iterable[str], preserve: frozenset[str]
) -> TreeStopPlan:
    present = tuple(units)
    stopping = tuple(unit_id for unit_id in present if unit_id not in preserve)
    kept = len(stopping) < len(present)
    if not stopping and kept:
        return LeaveTree()
    if stopping and _is_unit_map(units) and (waves := _plan_tiered_waves(units, stopping)):
        ordered = tuple(unit_id for wave in waves for unit_id in wave)
        return StopUnits(ordered, stop_root=not kept, waves=waves)
    return StopUnits(stopping, stop_root=not kept)
