# meiva:note
# One manifest entry, one row: create a missing row with the manifest's
# `default_enabled`, resync a drifted script and command, and never touch an
# existing row's enabled state, description or status.
# meiva:link base/daemon/schedules/docs/builtin-provisioning.meiva.okf.md
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

from base.db.rows import IdRow, query_all, query_one
from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_of
from base.paths import repo_root

# The manifest ships in the repo checkout every deployed unit runs from
# ($MEIVA_HOME/source on prod), so repo_root() resolves it in prod and in dev
# worktrees alike.
MANIFEST_PATH = repo_root() / "schedules" / "manifest.json"

# The two manifest classes — product (default enabled) and operator (default
# disabled). Unknown classes fail fast rather than guessing a default.
_BUILTIN_CLASSES = frozenset({"product", "operator"})
_BUILTIN_PLACEMENTS = frozenset({"gateway", "per_machine"})


class ManifestError(ValueError):
    """The built-in schedules manifest is malformed — missing fields, an
    unknown class, or a script file that does not sit beside the manifest."""


@dataclass(frozen=True)
class BuiltinSchedule:
    """One manifest entry — a schedule that ships with Meiva."""

    name: str
    klass: str  # "product" | "operator"
    default_enabled: bool
    description: str
    script: str
    command: str
    placement: str = "gateway"


# meiva:note
# The columns the drift check of one existing schedule row reads.
@dataclass(frozen=True)
class _ScheduleCodeRow:
    id: int
    script: str
    command: str
    machine: str


# meiva:note
# `_ScheduleCodeRow` plus the row's name, for the per-machine sweep over `name:%` rows.
@dataclass(frozen=True)
class _NamedScheduleCodeRow:
    id: int
    name: str
    script: str
    command: str
    machine: str


# meiva:note
# One `machines.name` read.
@dataclass(frozen=True)
class _MachineNameRow:
    name: str


def _parse_manifest_entry(item: object, manifest_dir: Path) -> BuiltinSchedule:
    if not is_object_dict(item):
        raise ManifestError(f"malformed manifest entry: {item!r} — expected an object")
    entry = item
    name = entry.get("name")
    klass = entry.get("class")
    default_enabled = entry.get("default_enabled")
    description = entry.get("description", "")
    script = entry.get("script")
    command = entry.get("command")
    placement = entry.get("placement", "gateway")
    if not isinstance(name, str) or not name:
        raise ManifestError(f"malformed manifest entry: {entry!r} — missing 'name'")
    if not isinstance(klass, str) or klass not in _BUILTIN_CLASSES:
        raise ManifestError(
            f"manifest schedule {name!r}: unknown class {klass!r} (expected {sorted(_BUILTIN_CLASSES)})"
        )
    if not isinstance(default_enabled, bool):
        raise ManifestError(f"manifest schedule {name!r}: 'default_enabled' must be a bool")
    if not isinstance(description, str):
        raise ManifestError(f"manifest schedule {name!r}: 'description' must be a string")
    if not isinstance(script, str) or not isinstance(command, str):
        raise ManifestError(f"manifest schedule {name!r}: 'script' and 'command' are required")
    if not isinstance(placement, str) or placement not in _BUILTIN_PLACEMENTS:
        raise ManifestError(
            f"manifest schedule {name!r}: unknown placement {placement!r} "
            f"(expected {sorted(_BUILTIN_PLACEMENTS)})"
        )
    script_file = manifest_dir / script
    if not script_file.is_file():
        raise ManifestError(f"manifest schedule {name!r}: script file {script_file} does not exist")
    return BuiltinSchedule(
        name=name,
        klass=klass,
        default_enabled=default_enabled,
        description=description,
        script=script,
        command=command,
        placement=placement,
    )


def load_manifest(path: Path | None = None) -> list[BuiltinSchedule]:
    """Parse and validate the built-in schedules manifest.

    Raises:
        FileNotFoundError: no manifest at ``path`` (or the default).
        ManifestError: malformed manifest — missing fields, unknown class, or
            a script file that does not sit beside the manifest.
    """
    manifest_path = path or MANIFEST_PATH
    with manifest_path.open() as f:
        payload = object_of(json.load, f)
    if not is_object_dict(payload):
        raise ManifestError(f"malformed manifest {manifest_path}: expected a JSON object")
    entries_raw = payload.get("builtin_schedules")
    if not is_object_list(entries_raw):
        raise ManifestError(
            f"malformed manifest {manifest_path}: expected {{builtin_schedules: [...]}}"
        )
    return [_parse_manifest_entry(item, manifest_path.parent) for item in entries_raw]


def template_for_schedule_name(
    name: str, manifest: Sequence[BuiltinSchedule]
) -> BuiltinSchedule | None:
    for entry in manifest:
        if entry.placement == "gateway" and entry.name == name:
            return entry
        if entry.placement == "per_machine":
            prefix = f"{entry.name}:"
            if name.startswith(prefix) and len(name) > len(prefix):
                return entry
    return None


def runner_machine_names(conn: psycopg.Connection, *, fallback_machine: str) -> tuple[str, ...]:
    rows = query_all(
        conn,
        _MachineNameRow,
        "SELECT name FROM machines WHERE 'agent-runner' = ANY(role) ORDER BY name",
    )
    return tuple(r.name for r in rows) if rows else (fallback_machine,)


def _no_names() -> list[str]:
    return []


@dataclass(frozen=True)
class ProvisionResult:
    """What one provision changed, in manifest order."""

    created: list[str] = field(default_factory=_no_names)
    resynced: list[str] = field(default_factory=_no_names)


def _digest(script: str, command: str) -> str:
    return hashlib.sha256(f"{command}\0{script}".encode()).hexdigest()[:12]


def _provision_gateway_schedule(
    conn: psycopg.Connection,
    sched: BuiltinSchedule,
    *,
    script_text: str,
    want: str,
    machine: str,
    result: ProvisionResult,
) -> None:
    existing = query_one(
        conn,
        _ScheduleCodeRow,
        "SELECT id, script, command, machine FROM schedules WHERE name = %s FOR UPDATE",
        (sched.name,),
    )
    if existing is None:
        row = query_one(
            conn,
            IdRow,
            "INSERT INTO schedules (name, description, script, command, enabled, machine) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (
                sched.name,
                sched.description,
                script_text,
                sched.command,
                sched.default_enabled,
                machine,
            ),
        )
        if row is None:
            raise RuntimeError(f"failed to insert schedule {sched.name}")
        conn.execute(
            "INSERT INTO schedule_versions (schedule_id, script, command, note) "
            "VALUES (%s, %s, %s, %s)",
            (row.id, script_text, sched.command, "initial"),
        )
        result.created.append(sched.name)
        return

    if _digest(existing.script, existing.command) == want:
        return
    conn.execute(
        "UPDATE schedules SET script = %s, command = %s, desired_revision = desired_revision + 1, "
        "launch_count = 0, next_launch_at = NULL, updated_at = now() WHERE id = %s",
        (script_text, sched.command, existing.id),
    )
    conn.execute(
        "INSERT INTO schedule_versions (schedule_id, script, command, note) "
        "VALUES (%s, %s, %s, %s)",
        (existing.id, script_text, sched.command, f"builtin-resync {want}"),
    )
    conn.execute(
        "INSERT INTO schedule_sync_requests (schedule_id, machine) VALUES (%s, %s) "
        "ON CONFLICT (schedule_id, machine) DO UPDATE SET requested_at = clock_timestamp()",
        (existing.id, existing.machine),
    )
    result.resynced.append(sched.name)


def _provision_per_machine_schedule(
    conn: psycopg.Connection,
    sched: BuiltinSchedule,
    *,
    script_text: str,
    want: str,
    fallback_machine: str,
    result: ProvisionResult,
) -> None:
    targets = runner_machine_names(conn, fallback_machine=fallback_machine)
    for target_machine in targets:
        row_name = f"{sched.name}:{target_machine}"
        existing = query_one(
            conn, IdRow, "SELECT id FROM schedules WHERE name = %s FOR UPDATE", (row_name,)
        )
        if existing is None:
            row = query_one(
                conn,
                IdRow,
                "INSERT INTO schedules (name, description, script, command, enabled, machine) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    row_name,
                    sched.description,
                    script_text,
                    sched.command,
                    sched.default_enabled,
                    target_machine,
                ),
            )
            if row is None:
                raise RuntimeError(f"failed to insert schedule {row_name}")
            conn.execute(
                "INSERT INTO schedule_versions (schedule_id, script, command, note) "
                "VALUES (%s, %s, %s, %s)",
                (row.id, script_text, sched.command, "initial"),
            )
            if sched.default_enabled:
                conn.execute(
                    "INSERT INTO schedule_sync_requests (schedule_id, machine) VALUES (%s, %s) "
                    "ON CONFLICT (schedule_id, machine) DO UPDATE SET requested_at = clock_timestamp()",
                    (row.id, target_machine),
                )
            result.created.append(row_name)

    for drifted in query_all(
        conn,
        _NamedScheduleCodeRow,
        "SELECT id, name, script, command, machine FROM schedules "
        "WHERE name LIKE %s AND owner_plugin IS NULL ORDER BY id FOR UPDATE",
        (f"{sched.name}:%",),
    ):
        if _digest(drifted.script, drifted.command) == want:
            continue
        conn.execute(
            "UPDATE schedules SET script = %s, command = %s, desired_revision = desired_revision + 1, "
            "launch_count = 0, next_launch_at = NULL, updated_at = now() WHERE id = %s",
            (script_text, sched.command, drifted.id),
        )
        conn.execute(
            "INSERT INTO schedule_versions (schedule_id, script, command, note) "
            "VALUES (%s, %s, %s, %s)",
            (drifted.id, script_text, sched.command, f"builtin-resync {want}"),
        )
        conn.execute(
            "INSERT INTO schedule_sync_requests (schedule_id, machine) VALUES (%s, %s) "
            "ON CONFLICT (schedule_id, machine) DO UPDATE SET requested_at = clock_timestamp()",
            (drifted.id, drifted.machine),
        )
        result.resynced.append(drifted.name)


def provision_builtin_schedules(
    conn: psycopg.Connection, *, machine: str, path: Path | None = None
) -> ProvisionResult:
    manifest = load_manifest(path)
    manifest_path = (path or MANIFEST_PATH).parent
    result = ProvisionResult()
    for sched in manifest:
        script_text = (manifest_path / sched.script).read_text()
        want = _digest(script_text, sched.command)
        if sched.placement == "gateway":
            _provision_gateway_schedule(
                conn,
                sched,
                script_text=script_text,
                want=want,
                machine=machine,
                result=result,
            )
        else:
            _provision_per_machine_schedule(
                conn,
                sched,
                script_text=script_text,
                want=want,
                fallback_machine=machine,
                result=result,
            )
    return result
