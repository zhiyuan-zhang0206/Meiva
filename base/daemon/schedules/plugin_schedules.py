# meiva:note
# Reconciliation of declarative plugin schedules into durable schedules rows.
# meiva:link docs/conventions/extensions/plugin-spec-v2.md

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from base.clock.recurring import RecurringTrigger
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
    status: str
    trigger_kind: str
    cron_expr: str | None
    interval_seconds: int | None
    timezone: str
    timeout_seconds: int
    next_launch_at: datetime | None


@dataclass(frozen=True)
class _ScheduleUpdate:
    trigger: RecurringTrigger
    timezone: str
    machine_changed: bool
    code_changed: bool
    trigger_changed: bool
    changed: bool


def _resolved_trigger(spec: ScheduleSpec, cluster_timezone: str) -> RecurringTrigger:
    trigger = RecurringTrigger(
        kind=spec.trigger_kind,
        cron_expr=spec.cron_expr,
        interval_seconds=spec.interval_seconds,
        timezone=spec.timezone or cluster_timezone,
        timeout_seconds=spec.timeout_seconds,
    )
    trigger.validate()
    return trigger


def _create_plugin_schedule(
    conn: psycopg.Connection,
    plugin: str,
    spec: ScheduleSpec,
    *,
    cluster_timezone: str,
    now: datetime,
) -> None:
    trigger = _resolved_trigger(spec, cluster_timezone)
    next_launch = (
        trigger.next_launch(now=now, cluster_timezone=cluster_timezone)
        if spec.default_enabled
        else None
    )
    timezone = trigger.timezone
    inserted = query_one(
        conn,
        IdRow,
        "INSERT INTO schedules "
        "(name, description, script, command, enabled, status, machine, owner_plugin, "
        "secret_env_vars, trigger_kind, cron_expr, interval_seconds, timezone, timeout_seconds, "
        "on_owner_terminate, next_launch_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'keep', %s) "
        "RETURNING id",
        (
            spec.name,
            spec.description,
            spec.script,
            spec.command,
            spec.default_enabled,
            "scheduled" if spec.default_enabled else "stopped",
            spec.machine,
            plugin,
            list(spec.secret_env_vars),
            spec.trigger_kind,
            spec.cron_expr,
            spec.interval_seconds,
            timezone,
            spec.timeout_seconds,
            next_launch,
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


def _schedule_update(
    row: _ScheduleRow, spec: ScheduleSpec, *, cluster_timezone: str
) -> _ScheduleUpdate:
    trigger = _resolved_trigger(spec, cluster_timezone)
    timezone = trigger.timezone or cluster_timezone
    machine_changed = row.machine != spec.machine
    code_changed = any((row.script != spec.script, row.command != spec.command))
    trigger_changed = any(
        (
            row.trigger_kind != spec.trigger_kind,
            row.cron_expr != spec.cron_expr,
            row.interval_seconds != spec.interval_seconds,
            row.timezone != timezone,
            row.timeout_seconds != spec.timeout_seconds,
        )
    )
    changed = any(
        (
            machine_changed,
            row.description != spec.description,
            code_changed,
            tuple(row.secret_env_vars) != spec.secret_env_vars,
            trigger_changed,
        )
    )
    return _ScheduleUpdate(
        trigger=trigger,
        timezone=timezone,
        machine_changed=machine_changed,
        code_changed=code_changed,
        trigger_changed=trigger_changed,
        changed=changed,
    )


def _runtime_after_update(
    row: _ScheduleRow,
    update: _ScheduleUpdate,
    *,
    cluster_timezone: str,
    now: datetime,
) -> tuple[datetime | None, str]:
    if not update.trigger_changed:
        return row.next_launch_at, row.status
    if row.enabled:
        return (
            update.trigger.next_launch(now=now, cluster_timezone=cluster_timezone),
            "scheduled",
        )
    return None, "stopped"


def _queue_sync(conn: psycopg.Connection, schedule_id: int, machine: str) -> None:
    conn.execute(
        "INSERT INTO schedule_sync_requests (schedule_id, machine) VALUES (%s, %s) "
        "ON CONFLICT (schedule_id, machine) DO UPDATE SET requested_at = now()",
        (schedule_id, machine),
    )


def _owner_conflicts(plugin: str, spec: ScheduleSpec, row: _ScheduleRow) -> bool:
    if row.owner_plugin == plugin:
        return False
    load_report.report_plugin_load_failure(
        plugin,
        RuntimeError(
            f"schedule {spec.name!r} already exists with owner_plugin={row.owner_plugin!r}"
        ),
    )
    return True


def _update_plugin_schedule(
    conn: psycopg.Connection,
    plugin: str,
    spec: ScheduleSpec,
    row: _ScheduleRow,
    *,
    cluster_timezone: str,
    now: datetime,
) -> tuple[bool, bool]:
    sched_id = row.id
    if _owner_conflicts(plugin, spec, row):
        return False, True

    update = _schedule_update(row, spec, cluster_timezone=cluster_timezone)
    if not update.changed:
        return False, False

    next_launch, status = _runtime_after_update(
        row, update, cluster_timezone=cluster_timezone, now=now
    )

    conn.execute(
        "UPDATE schedules SET machine = %s, description = %s, script = %s, command = %s, "
        "secret_env_vars = %s, trigger_kind = %s, run_at = NULL, cron_expr = %s, "
        "interval_seconds = %s, timezone = %s, timeout_seconds = %s, "
        "on_owner_terminate = 'keep', status = %s, last_error = NULL, next_launch_at = %s, "
        "updated_at = %s, desired_revision = desired_revision + 1, launch_count = 0, "
        "launch_generation = CASE WHEN %s THEN NULL ELSE launch_generation END "
        "WHERE id = %s",
        (
            spec.machine,
            spec.description,
            spec.script,
            spec.command,
            list(spec.secret_env_vars),
            spec.trigger_kind,
            spec.cron_expr,
            spec.interval_seconds,
            update.timezone,
            spec.timeout_seconds,
            status,
            next_launch,
            now,
            update.machine_changed,
            sched_id,
        ),
    )
    if update.code_changed:
        conn.execute(
            "INSERT INTO schedule_versions (schedule_id, script, command, note) "
            "VALUES (%s, %s, %s, 'plugin-resync')",
            (sched_id, spec.script, spec.command),
        )
    _queue_sync(conn, sched_id, spec.machine)
    if update.machine_changed:
        _queue_sync(conn, sched_id, row.machine)
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
    conn: psycopg.Connection,
    *,
    home: Path,
    cluster_timezone: str,
    now: datetime,
) -> PluginScheduleSyncSummary:
    effective_now = now.astimezone(UTC)
    specs_by_plugin, failed_plugins = load_enabled_plugin_schedules(home=home)

    existing_rows = query_all(
        conn,
        _ScheduleRow,
        "SELECT id, name, description, script, command, enabled, machine, owner_plugin, "
        "secret_env_vars, status, trigger_kind, cron_expr, interval_seconds, timezone, "
        "timeout_seconds, next_launch_at "
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
            _create_plugin_schedule(
                conn,
                plugin,
                spec,
                cluster_timezone=cluster_timezone,
                now=effective_now,
            )
            created.append(spec.name)
        else:
            was_updated, was_skipped = _update_plugin_schedule(
                conn,
                plugin,
                spec,
                existing_by_name[spec.name],
                cluster_timezone=cluster_timezone,
                now=effective_now,
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
