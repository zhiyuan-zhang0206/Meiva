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
from typing import Any, TypeGuard

import psycopg

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


def _is_str_dict(value: object) -> TypeGuard[dict[str, Any]]:
    return isinstance(value, dict)


def _is_any_list(value: object) -> TypeGuard[list[Any]]:
    return isinstance(value, list)


def _parse_manifest_entry(item: object, manifest_dir: Path) -> BuiltinSchedule:
    if not _is_str_dict(item):
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
    if klass not in _BUILTIN_CLASSES:
        raise ManifestError(
            f"manifest schedule {name!r}: unknown class {klass!r} (expected {sorted(_BUILTIN_CLASSES)})"
        )
    if not isinstance(default_enabled, bool):
        raise ManifestError(f"manifest schedule {name!r}: 'default_enabled' must be a bool")
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
        payload: object = json.load(f)
    if not _is_str_dict(payload):
        raise ManifestError(f"malformed manifest {manifest_path}: expected a JSON object")
    entries_raw = payload.get("builtin_schedules")
    if not _is_any_list(entries_raw):
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


def runner_machine_names(
    conn: psycopg.Connection[Any], *, fallback_machine: str
) -> tuple[str, ...]:
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM machines WHERE 'agent-runner' = ANY(role) ORDER BY name")
        rows = cur.fetchall()
    return tuple(r[0] for r in rows) if rows else (fallback_machine,)


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
    cur: psycopg.Cursor[Any],
    sched: BuiltinSchedule,
    *,
    script_text: str,
    want: str,
    machine: str,
    result: ProvisionResult,
) -> None:
    cur.execute(
        "SELECT id, script, command, machine FROM schedules WHERE name = %s FOR UPDATE",
        (sched.name,),
    )
    existing = cur.fetchone()
    if existing is None:
        cur.execute(
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
        row = cur.fetchone()
        if row is None:
            raise RuntimeError(f"failed to insert schedule {sched.name}")
        cur.execute(
            "INSERT INTO schedule_versions (schedule_id, script, command, note) "
            "VALUES (%s, %s, %s, %s)",
            (row[0], script_text, sched.command, "initial"),
        )
        result.created.append(sched.name)
        return

    schedule_id, current_script, current_command, existing_machine = existing
    if _digest(current_script or "", current_command or "") == want:
        return
    cur.execute(
        "UPDATE schedules SET script = %s, command = %s, desired_revision = desired_revision + 1, "
        "launch_count = 0, next_launch_at = NULL, updated_at = now() WHERE id = %s",
        (script_text, sched.command, schedule_id),
    )
    cur.execute(
        "INSERT INTO schedule_versions (schedule_id, script, command, note) "
        "VALUES (%s, %s, %s, %s)",
        (schedule_id, script_text, sched.command, f"builtin-resync {want}"),
    )
    cur.execute(
        "INSERT INTO schedule_sync_requests (schedule_id, machine) VALUES (%s, %s) "
        "ON CONFLICT (schedule_id, machine) DO UPDATE SET requested_at = clock_timestamp()",
        (schedule_id, existing_machine),
    )
    result.resynced.append(sched.name)


def _provision_per_machine_schedule(
    conn: psycopg.Connection[Any],
    sched: BuiltinSchedule,
    *,
    script_text: str,
    want: str,
    fallback_machine: str,
    result: ProvisionResult,
) -> None:
    targets = runner_machine_names(conn, fallback_machine=fallback_machine)
    with conn.cursor() as cur:
        for target_machine in targets:
            row_name = f"{sched.name}:{target_machine}"
            cur.execute("SELECT id FROM schedules WHERE name = %s FOR UPDATE", (row_name,))
            existing = cur.fetchone()
            if existing is None:
                cur.execute(
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
                row = cur.fetchone()
                if row is None:
                    raise RuntimeError(f"failed to insert schedule {row_name}")
                schedule_id = row[0]
                cur.execute(
                    "INSERT INTO schedule_versions (schedule_id, script, command, note) "
                    "VALUES (%s, %s, %s, %s)",
                    (schedule_id, script_text, sched.command, "initial"),
                )
                if sched.default_enabled:
                    cur.execute(
                        "INSERT INTO schedule_sync_requests (schedule_id, machine) VALUES (%s, %s) "
                        "ON CONFLICT (schedule_id, machine) DO UPDATE SET requested_at = clock_timestamp()",
                        (schedule_id, target_machine),
                    )
                result.created.append(row_name)

        cur.execute(
            "SELECT id, name, script, command, machine FROM schedules "
            "WHERE name LIKE %s AND owner_plugin IS NULL ORDER BY id FOR UPDATE",
            (f"{sched.name}:%",),
        )
        for (
            schedule_id,
            row_name,
            current_script,
            current_command,
            existing_machine,
        ) in cur.fetchall():
            if _digest(current_script or "", current_command or "") == want:
                continue
            cur.execute(
                "UPDATE schedules SET script = %s, command = %s, desired_revision = desired_revision + 1, "
                "launch_count = 0, next_launch_at = NULL, updated_at = now() WHERE id = %s",
                (script_text, sched.command, schedule_id),
            )
            cur.execute(
                "INSERT INTO schedule_versions (schedule_id, script, command, note) "
                "VALUES (%s, %s, %s, %s)",
                (schedule_id, script_text, sched.command, f"builtin-resync {want}"),
            )
            cur.execute(
                "INSERT INTO schedule_sync_requests (schedule_id, machine) VALUES (%s, %s) "
                "ON CONFLICT (schedule_id, machine) DO UPDATE SET requested_at = clock_timestamp()",
                (schedule_id, existing_machine),
            )
            result.resynced.append(row_name)


def provision_builtin_schedules(
    conn: psycopg.Connection[Any], *, machine: str, path: Path | None = None
) -> ProvisionResult:
    manifest = load_manifest(path)
    manifest_path = (path or MANIFEST_PATH).parent
    result = ProvisionResult()
    for sched in manifest:
        script_text = (manifest_path / sched.script).read_text()
        want = _digest(script_text, sched.command)
        if sched.placement == "gateway":
            with conn.cursor() as cur:
                _provision_gateway_schedule(
                    cur,
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
