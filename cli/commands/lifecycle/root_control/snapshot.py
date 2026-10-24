# meiva:note
# The root's status snapshot as a validated, typed value.
#
# The wire layer already checks that a status reply is a unit roster with known unit
# states. `parse_snapshot` checks the rest of what the lifecycle commands read, once, at
# the boundary, so every decision below it works with typed rows instead of casting JSON.
# A reply that lacks a field the commands depend on is refused here rather than carried
# on as a half-valid dict.

from __future__ import annotations

from typing import NotRequired, TypedDict, cast

__all__ = [
    "HealthRow",
    "RootBlock",
    "RootSnapshot",
    "SnapshotError",
    "UnitRow",
    "parse_snapshot",
    "root_units",
]

_UNIT_TEXT_FIELDS = ("intent", "manifest_digest", "last_error", "last_exit")


# meiva:note
# A status reply does not have the shape the lifecycle commands read.
class SnapshotError(ValueError): ...


# meiva:note
# The root daemon's own native birth and the digest of the launch it serves.
class RootBlock(TypedDict):
    pid: int
    create_time: float
    starttime: int | None
    launch_digest: str | None


# meiva:note
# One unit of the tree: what its process does (`state`) and what the supervisor
# intends for it (`intent`); the other fields are present when the root reports them.
class UnitRow(TypedDict):
    id: str
    state: str
    intent: NotRequired[str]
    manifest_digest: NotRequired[str]
    last_error: NotRequired[str]
    last_exit: NotRequired[str]


# meiva:note
# The health monitor's restart-budget fact for one unit.
class HealthRow(TypedDict):
    restart_limit_reached: bool


# meiva:note
# One reading of the whole tree. `health` is absent while the monitor is not up.
class RootSnapshot(TypedDict):
    root: RootBlock
    units: list[UnitRow]
    health: NotRequired[dict[str, HealthRow]]


# meiva:note
# Validate the `result` of a successful status reply.
#
# Raises:
#     SnapshotError: a field the lifecycle commands read is missing or mistyped.
def parse_snapshot(result: object) -> RootSnapshot:
    body = _object(result, "root status")
    items = body.get("units")
    if not isinstance(items, list):
        raise SnapshotError("root status omitted its unit roster")
    snapshot: RootSnapshot = {
        "root": _root_block(body.get("root")),
        "units": [_unit_row(item) for item in cast("list[object]", items)],
    }
    health = body.get("health")
    if health is not None:
        snapshot["health"] = _health(health)
    return snapshot


# meiva:note
# Unit rows by unit id, in the root's declaration order.
def root_units(snapshot: RootSnapshot) -> dict[str, UnitRow]:
    return {row["id"]: row for row in snapshot["units"]}


def _object(value: object, what: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise SnapshotError(f"{what} is not an object")
    return cast("dict[str, object]", value)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _root_block(value: object) -> RootBlock:
    row = _object(value, "root status root block")
    pid, birth, starttime = row.get("pid"), row.get("create_time"), row.get("starttime")
    digest = row.get("launch_digest")
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or isinstance(birth, bool)
        or not isinstance(birth, (int, float))
        or (starttime is not None and not _is_int(starttime))
        or (digest is not None and not isinstance(digest, str))
    ):
        raise SnapshotError("root status lacks the root's native identity")
    return {
        "pid": pid,
        "create_time": float(birth),
        "starttime": cast("int | None", starttime),
        "launch_digest": digest,
    }


def _unit_row(value: object) -> UnitRow:
    row = _object(value, "unit row")
    unit_id, state = row.get("id"), row.get("state")
    if not isinstance(unit_id, str) or not unit_id or not isinstance(state, str):
        raise SnapshotError("a unit row lacks its id or state")
    built: dict[str, object] = {"id": unit_id, "state": state}
    for field in _UNIT_TEXT_FIELDS:
        text = row.get(field)
        if text is None:
            continue
        if not isinstance(text, str):
            raise SnapshotError(f"unit {unit_id!r} reports a non-text {field}")
        built[field] = text
    return cast("UnitRow", built)


def _health(value: object) -> dict[str, HealthRow]:
    rows = _object(value, "root status health block")
    health: dict[str, HealthRow] = {}
    for unit_id, item in rows.items():
        if unit_id.startswith(("diagnostic:", "observer:")):
            continue
        reached = _object(item, f"health of {unit_id!r}").get("restart_limit_reached")
        if not isinstance(reached, bool):
            raise SnapshotError(f"health of {unit_id!r} lacks restart_limit_reached")
        health[unit_id] = {"restart_limit_reached": reached}
    return health
