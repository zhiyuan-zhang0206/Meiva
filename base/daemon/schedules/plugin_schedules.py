# meiva:note
# Reconciliation of declarative plugin schedules into durable schedules rows.
# meiva:link docs/conventions/extensions/plugin-spec-v2.md

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

from base.packages.plugins import load_report
from base.packages.plugins.loading.schedule_face import ScheduleSpec, load_enabled_plugin_schedules


@dataclass(frozen=True)
class PluginScheduleSyncSummary:
    created: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()


def _create_plugin_schedule(cur: psycopg.Cursor[Any], plugin: str, spec: ScheduleSpec) -> None:
    cur.execute(
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
    inserted = cur.fetchone()
    if inserted is None:
        raise RuntimeError("INSERT schedules RETURNING id returned no row")
    row_id: int = int(inserted[0])
    cur.execute(
        "INSERT INTO schedule_versions (schedule_id, script, command, note) "
        "VALUES (%s, %s, %s, 'plugin-initial')",
        (row_id, spec.script, spec.command),
    )
    if spec.default_enabled:
        cur.execute(
            "INSERT INTO schedule_sync_requests (schedule_id, machine) VALUES (%s, %s) "
            "ON CONFLICT (schedule_id, machine) DO UPDATE SET requested_at = now()",
            (row_id, spec.machine),
        )


def _update_plugin_schedule(
    cur: psycopg.Cursor[Any], plugin: str, spec: ScheduleSpec, row: tuple[Any, ...]
) -> tuple[bool, bool]:
    sched_id: int = int(row[0])
    existing_owner = row[7]
    if existing_owner != plugin:
        load_report.report_plugin_load_failure(
            plugin,
            RuntimeError(
                f"schedule {spec.name!r} already exists with owner_plugin={existing_owner!r}"
            ),
        )
        return False, True

    row_secrets = tuple(row[8] or ())
    machine_changed = row[6] != spec.machine
    code_changed = (row[3] != spec.script) or (row[4] != spec.command)
    changed = (
        machine_changed
        or (row[2] != spec.description)
        or code_changed
        or (row_secrets != spec.secret_env_vars)
    )
    if not changed:
        return False, False

    old_machine = row[6]
    is_enabled = bool(row[5])

    cur.execute(
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
        cur.execute(
            "INSERT INTO schedule_versions (schedule_id, script, command, note) "
            "VALUES (%s, %s, %s, 'plugin-resync')",
            (sched_id, spec.script, spec.command),
        )
    if is_enabled or machine_changed:
        cur.execute(
            "INSERT INTO schedule_sync_requests (schedule_id, machine) VALUES (%s, %s) "
            "ON CONFLICT (schedule_id, machine) DO UPDATE SET requested_at = now()",
            (sched_id, spec.machine),
        )
    if machine_changed:
        cur.execute(
            "INSERT INTO schedule_sync_requests (schedule_id, machine) VALUES (%s, %s) "
            "ON CONFLICT (schedule_id, machine) DO UPDATE SET requested_at = now()",
            (sched_id, old_machine),
        )
    return True, False


def _remove_orphan_schedules(
    cur: psycopg.Cursor[Any],
    existing_rows: list[tuple[Any, ...]],
    desired_by_name: dict[str, tuple[str, ScheduleSpec]],
    failed_plugins: set[str],
) -> list[str]:
    removed: list[str] = []
    for row in existing_rows:
        owner = row[7]
        if owner is None or owner in failed_plugins:
            continue
        name = row[1]
        if name not in desired_by_name:
            sched_id = row[0]
            machine = row[6]
            cur.execute("DELETE FROM schedules WHERE id = %s", (sched_id,))
            cur.execute(
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
    conn: psycopg.Connection[Any], *, home: Path
) -> PluginScheduleSyncSummary:
    specs_by_plugin, failed_plugins = load_enabled_plugin_schedules(home=home)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, description, script, command, enabled, machine, owner_plugin, secret_env_vars "
            "FROM schedules FOR UPDATE"
        )
        existing_rows = cur.fetchall()

    existing_by_name: dict[str, tuple[Any, ...]] = {r[1]: r for r in existing_rows}
    desired_by_name: dict[str, tuple[str, ScheduleSpec]] = {}
    for plugin in sorted(specs_by_plugin):
        for spec in specs_by_plugin[plugin]:
            desired_by_name[spec.name] = (plugin, spec)

    created: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []

    with conn.cursor() as cur:
        for plugin, spec in desired_by_name.values():
            if spec.name not in existing_by_name:
                _create_plugin_schedule(cur, plugin, spec)
                created.append(spec.name)
            else:
                was_updated, was_skipped = _update_plugin_schedule(
                    cur, plugin, spec, existing_by_name[spec.name]
                )
                if was_updated:
                    updated.append(spec.name)
                elif was_skipped:
                    skipped.append(spec.name)

        removed = _remove_orphan_schedules(cur, existing_rows, desired_by_name, failed_plugins)

    return PluginScheduleSyncSummary(
        created=tuple(created),
        updated=tuple(updated),
        removed=tuple(removed),
        skipped=tuple(skipped),
    )

    return PluginScheduleSyncSummary(
        created=tuple(created),
        updated=tuple(updated),
        removed=tuple(removed),
        skipped=tuple(skipped),
    )
