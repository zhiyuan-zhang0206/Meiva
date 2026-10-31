# meiva:note
# One manifest entry, one row: create a missing row with the manifest's
# `default_enabled`, resync a drifted script and command, and never touch an
# existing row's enabled state, description or status.
# meiva:link base/daemon/schedules/docs/builtin-provisioning.meiva.okf.md
from __future__ import annotations

import hashlib
import json
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


def _is_str_dict(value: object) -> TypeGuard[dict[str, Any]]:
    return isinstance(value, dict)


def _is_any_list(value: object) -> TypeGuard[list[Any]]:
    return isinstance(value, list)


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
    schedules: list[BuiltinSchedule] = []
    for item in entries_raw:
        if not _is_str_dict(item):
            raise ManifestError(f"malformed manifest entry: {item!r} — expected an object")
        entry = item
        name = entry.get("name")
        klass = entry.get("class")
        default_enabled = entry.get("default_enabled")
        description = entry.get("description", "")
        script = entry.get("script")
        command = entry.get("command")
        if not isinstance(name, str) or not name:
            raise ManifestError(f"malformed manifest entry: {entry!r} — missing 'name'")
        if klass not in _BUILTIN_CLASSES:
            raise ManifestError(
                f"manifest schedule {name!r}: unknown class {klass!r} "
                f"(expected {sorted(_BUILTIN_CLASSES)})"
            )
        if not isinstance(default_enabled, bool):
            raise ManifestError(f"manifest schedule {name!r}: 'default_enabled' must be a bool")
        if not isinstance(script, str) or not isinstance(command, str):
            raise ManifestError(f"manifest schedule {name!r}: 'script' and 'command' are required")
        script_file = manifest_path.parent / script
        if not script_file.is_file():
            raise ManifestError(
                f"manifest schedule {name!r}: script file {script_file} does not exist"
            )
        schedules.append(
            BuiltinSchedule(
                name=name,
                klass=klass,
                default_enabled=default_enabled,
                description=description,
                script=script,
                command=command,
            )
        )
    return schedules


def _no_names() -> list[str]:
    return []


@dataclass(frozen=True)
class ProvisionResult:
    """What one provision changed, in manifest order."""

    created: list[str] = field(default_factory=_no_names)
    resynced: list[str] = field(default_factory=_no_names)


def _digest(script: str, command: str) -> str:
    return hashlib.sha256(f"{command}\0{script}".encode()).hexdigest()[:12]


def provision_builtin_schedules(
    conn: psycopg.Connection[Any], *, machine: str, path: Path | None = None
) -> ProvisionResult:
    manifest = load_manifest(path)
    manifest_path = (path or MANIFEST_PATH).parent
    result = ProvisionResult()
    for sched in manifest:
        # The manifest names a template file; the DB stores the script text
        # (the runner materializes it to $MEIVA_HOME/schedules/<id>/).
        script_text = (manifest_path / sched.script).read_text()
        with conn.cursor() as cur:
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
                assert row is not None  # noqa: S101 — INSERT ... RETURNING always yields a row
                # Same "initial" version snapshot the API create writes, so a
                # provisioned built-in carries the same roll-back history shape.
                cur.execute(
                    "INSERT INTO schedule_versions (schedule_id, script, command, note) "
                    "VALUES (%s, %s, %s, %s)",
                    (row[0], script_text, sched.command, "initial"),
                )
                result.created.append(sched.name)
                continue
            schedule_id, current_script, current_command, existing_machine = existing
            want = _digest(script_text, sched.command)
            if _digest(current_script or "", current_command or "") == want:
                continue
            cur.execute(
                "UPDATE schedules SET script = %s, command = %s, desired_revision = desired_revision + 1, launch_count = 0, next_launch_at = NULL, updated_at = now() WHERE id = %s",
                (script_text, sched.command, schedule_id),
            )
            cur.execute(
                "INSERT INTO schedule_versions (schedule_id, script, command, note) "
                "VALUES (%s, %s, %s, %s)",
                (schedule_id, script_text, sched.command, f"builtin-resync {want}"),
            )
            # Same request the API leaves on a script edit: the schedule-manager
            # kills a live session and relaunches it (when enabled) on the new code.
            cur.execute(
                "INSERT INTO schedule_sync_requests (schedule_id, machine) VALUES (%s, %s) "
                "ON CONFLICT (schedule_id, machine) DO UPDATE SET requested_at = clock_timestamp()",
                (schedule_id, existing_machine),
            )
        result.resynced.append(sched.name)
    return result
