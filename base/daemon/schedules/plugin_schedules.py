# meiva:note
# Reconciliation of declarative plugin schedules into durable schedules rows.
# meiva:link docs/conventions/extensions/plugin-spec-v2.md

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import psycopg

from base.db.rows import IdRow, query_all, query_one
from base.packages.plugins import load_report
from base.packages.plugins.loading.schedule_face import ScheduleSpec, load_enabled_plugin_schedules


@dataclass(frozen=True)
class PluginScheduleSyncSummary:
    created: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()


# meiva:note
# One existing `schedules` row as the plugin reconciliation reads it.
@dataclass(frozen=True)
class _ScheduleRow:
    id: int
    name: str
    description: str | None
    script: str
    command: str
    enabled: bool
    machine: str
    owner_plugin: str | None
    secret_env_vars: Sequence[str]


def _create_plugin_schedule(conn: psycopg.Connection, plugin: str, spec: ScheduleSpec) -> None:
    inserted = query_one(
        conn,
        IdRow,
        "INSERT INTO schedules "
        "(name, description, script, command, enabled, machine, owner_plugin, secret_env_vars) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (
            spec.name,
            spec.description,
            spec.script,
            spec.command,
            spec.default_enabled,
            spec.machine,
            plugin,
            list(spec.secret_env_vars),
        ),
    )
    if inserted is None:
        raise RuntimeError("INSERT schedules RETURNING id returned no row")
    row_id = inserted.id
    conn.execute(
        "INSERT INTO schedule_versions (schedule_id, script, command, note) "
        "VALUES (%s, %s, %s, 'plugin-initial')",
        (row_id, spec.script, spec.command),
    )
    if spec.default_enabled:
        conn.execute(
            "INSERT INTO schedule_sync_requests (schedule_id, machine) VALUES (%s, %s) "
            "ON CONFLICT (schedule_id, machine) DO UPDATE SET requested_at = now()",
            (row_id, spec.machine),
        )


def _update_plugin_schedule(
    conn: psycopg.Connection, plugin: str, spec: ScheduleSpec, row: _ScheduleRow
) -> tuple[bool, bool]:
    sched_id = row.id
    existing_owner = row.owner_plugin
    if existing_owner != plugin:
        load_report.report_plugin_load_failure(
            plugin,
            RuntimeError(
                f"schedule {spec.name!r} already exists with owner_plugin={existing_owner!r}"
            ),
        )
        return False, True

    row_secrets = tuple(row.secret_env_vars)
    machine_changed = row.machine != spec.machine
    code_changed = (row.script != spec.script) or (row.command != spec.command)
    changed = (
        machine_changed
        or (row.description != spec.description)
        or code_changed
        or (row_secrets != spec.secret_env_vars)
    )
    if not changed:
        return False, False

    old_machine = row.machine
    is_enabled = row.enabled

    conn.execute(
        "UPDATE schedules SET machine = %s, description = %s, script = %s, command = %s, "
        "secret_env_vars = %s, updated_at = now(), desired_revision = desired_revision + 1, "
        "launch_count = 0, next_launch_at = NULL, "
        "launch_generation = CASE WHEN %s THEN NULL ELSE launch_generation END "
        "WHERE id = %s",
        (
            spec.machine,
            spec.description,
            spec.script,
            spec.command,
            list(spec.secret_env_vars),
            machine_changed,
            sched_id,
        ),
    )
    if code_changed:
        conn.execute(
            "INSERT INTO schedule_versions (schedule_id, script, command, note) "
            "VALUES (%s, %s, %s, 'plugin-resync')",
            (sched_id, spec.script, spec.command),
        )
    if is_enabled or machine_changed:
        conn.execute(
            "INSERT INTO schedule_sync_requests (schedule_id, machine) VALUES (%s, %s) "
            "ON CONFLICT (schedule_id, machine) DO UPDATE SET requested_at = now()",
            (sched_id, spec.machine),
        )
    if machine_changed:
        conn.execute(
            "INSERT INTO schedule_sync_requests (schedule_id, machine) VALUES (%s, %s) "
            "ON CONFLICT (schedule_id, machine) DO UPDATE SET requested_at = now()",
            (sched_id, old_machine),
        )
    return True, False


def _remove_orphan_schedules(
    conn: psycopg.Connection,
    existing_rows: list[_ScheduleRow],
    desired_by_name: dict[str, tuple[str, ScheduleSpec]],
    failed_plugins: set[str],
) -> list[str]:
    removed: list[str] = []
    for row in existing_rows:
        owner = row.owner_plugin
        if owner is None or owner in failed_plugins:
            continue
        name = row.name
        if name not in desired_by_name:
            sched_id = row.id
            machine = row.machine
            conn.execute("DELETE FROM schedules WHERE id = %s", (sched_id,))
            conn.execute(
                "INSERT INTO schedule_sync_requests (schedule_id, machine) VALUES (%s, %s) "
                "ON CONFLICT (schedule_id, machine) DO UPDATE SET requested_at = now()",
                (sched_id, machine),
            )
            removed.append(name)
    return removed


# meiva:note
# Reconcile plugin-contributed schedules against the schedules table.
#
# Level-triggered:
# - Missing schedules declared by enabled plugins are created.
# - Updated scripts or commands append to schedule_versions and bump desired_revision.
# - Updated machine assignments reset launch_generation and notify both old and new nodes.
# - Existing operator enabled/disabled states are preserved.
# - Orphaned rows of disabled or uninstalled plugins are deleted and reaped.
# - If a plugin failed to load, its existing rows are kept intact rather than deleted.
# meiva:link docs/conventions/extensions/plugin-spec-v2.md
def reconcile_plugin_schedules(
    conn: psycopg.Connection, *, home: Path
) -> PluginScheduleSyncSummary:
    specs_by_plugin, failed_plugins = load_enabled_plugin_schedules(home=home)

    existing_rows = query_all(
        conn,
        _ScheduleRow,
        "SELECT id, name, description, script, command, enabled, machine, owner_plugin, secret_env_vars "
        "FROM schedules FOR UPDATE",
    )

    existing_by_name = {r.name: r for r in existing_rows}
    desired_by_name: dict[str, tuple[str, ScheduleSpec]] = {}
    for plugin in sorted(specs_by_plugin):
        for spec in specs_by_plugin[plugin]:
            desired_by_name[spec.name] = (plugin, spec)

    created: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []

    for plugin, spec in desired_by_name.values():
        if spec.name not in existing_by_name:
            _create_plugin_schedule(conn, plugin, spec)
            created.append(spec.name)
        else:
            was_updated, was_skipped = _update_plugin_schedule(
                conn, plugin, spec, existing_by_name[spec.name]
            )
            if was_updated:
                updated.append(spec.name)
            elif was_skipped:
                skipped.append(spec.name)

    removed = _remove_orphan_schedules(conn, existing_rows, desired_by_name, failed_plugins)

    return PluginScheduleSyncSummary(
        created=tuple(created),
        updated=tuple(updated),
        removed=tuple(removed),
        skipped=tuple(skipped),
    )
