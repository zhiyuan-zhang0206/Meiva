# meiva:note
# One manifest entry, one row: create a missing declarative row with the
# manifest's `default_enabled`, resync code and trigger policy, and preserve an
# existing row's operator-owned enabled state and description.
# meiva:link base/daemon/schedules/docs/builtin-provisioning.md
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from base.clock.recurring import RecurringTrigger, RecurringTriggerKind
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
    trigger_kind: RecurringTriggerKind = "cron"
    cron_expr: str | None = None
    interval_seconds: int | None = None
    timezone: str | None = None
    timeout_seconds: int = 600


# meiva:note
# The columns the drift check of one existing schedule row reads.
@dataclass(frozen=True)
class _StoredScheduleRow:
    id: int
    name: str
    script: str
    command: str
    machine: str
    enabled: bool
    trigger_kind: str
    cron_expr: str | None
    interval_seconds: int | None
    timezone: str
    timeout_seconds: int


# meiva:note
# One `machines.name` read.
@dataclass(frozen=True)
class _MachineNameRow:
    name: str


def _manifest_name(entry: dict[str, object]) -> str:
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise ManifestError(f"malformed manifest entry: {entry!r} — missing 'name'")
    return name


def _manifest_string(entry: dict[str, object], name: str, field_name: str, default: str) -> str:
    value = entry.get(field_name, default)
    if not isinstance(value, str):
        raise ManifestError(f"manifest schedule {name!r}: {field_name!r} must be a string")
    return value


def _optional_manifest_string(entry: dict[str, object], name: str, field_name: str) -> str | None:
    value = entry.get(field_name)
    if value is not None and not isinstance(value, str):
        raise ManifestError(f"manifest schedule {name!r}: {field_name} must be a string")
    return value


def _manifest_trigger(entry: dict[str, object], name: str) -> RecurringTrigger:
    trigger_kind = entry.get("trigger_kind")
    if trigger_kind not in ("cron", "every"):
        raise ManifestError(f"manifest schedule {name!r}: trigger_kind must be 'cron' or 'every'")
    interval_seconds = entry.get("interval_seconds")
    if interval_seconds is not None and not isinstance(interval_seconds, int):
        raise ManifestError(f"manifest schedule {name!r}: interval_seconds must be an integer")
    timeout_seconds = entry.get("timeout_seconds", 600)
    if not isinstance(timeout_seconds, int):
        raise ManifestError(f"manifest schedule {name!r}: timeout_seconds must be an integer")
    trigger = RecurringTrigger(
        kind=trigger_kind,
        cron_expr=_optional_manifest_string(entry, name, "cron_expr"),
        interval_seconds=interval_seconds,
        timezone=_optional_manifest_string(entry, name, "timezone"),
        timeout_seconds=timeout_seconds,
    )
    try:
        trigger.validate()
    except ValueError as exc:
        raise ManifestError(f"manifest schedule {name!r}: {exc}") from exc
    return trigger


def _manifest_class(entry: dict[str, object], name: str) -> str:
    klass = entry.get("class")
    if not isinstance(klass, str) or klass not in _BUILTIN_CLASSES:
        raise ManifestError(
            f"manifest schedule {name!r}: unknown class {klass!r} (expected {sorted(_BUILTIN_CLASSES)})"
        )
    return klass


def _manifest_placement(entry: dict[str, object], name: str) -> str:
    placement = entry.get("placement", "gateway")
    if not isinstance(placement, str) or placement not in _BUILTIN_PLACEMENTS:
        raise ManifestError(
            f"manifest schedule {name!r}: unknown placement {placement!r} "
            f"(expected {sorted(_BUILTIN_PLACEMENTS)})"
        )
    return placement


def _manifest_enabled(entry: dict[str, object], name: str) -> bool:
    enabled = entry.get("default_enabled")
    if not isinstance(enabled, bool):
        raise ManifestError(f"manifest schedule {name!r}: 'default_enabled' must be a bool")
    return enabled


def _parse_manifest_entry(item: object, manifest_dir: Path) -> BuiltinSchedule:
    if not is_object_dict(item):
        raise ManifestError(f"malformed manifest entry: {item!r} — expected an object")
    entry = item
    name = _manifest_name(entry)
    script = _manifest_string(entry, name, "script", "")
    command = _manifest_string(entry, name, "command", "")
    if not script or not command:
        raise ManifestError(f"manifest schedule {name!r}: 'script' and 'command' are required")
    trigger = _manifest_trigger(entry, name)
    script_file = manifest_dir / script
    if not script_file.is_file():
        raise ManifestError(f"manifest schedule {name!r}: script file {script_file} does not exist")
    return BuiltinSchedule(
        name=name,
        klass=_manifest_class(entry, name),
        default_enabled=_manifest_enabled(entry, name),
        description=_manifest_string(entry, name, "description", ""),
        script=script,
        command=command,
        placement=_manifest_placement(entry, name),
        trigger_kind=trigger.kind,
        cron_expr=trigger.cron_expr,
        interval_seconds=trigger.interval_seconds,
        timezone=trigger.timezone,
        timeout_seconds=trigger.timeout_seconds,
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


def _resolved_trigger(sched: BuiltinSchedule, cluster_timezone: str) -> RecurringTrigger:
    return RecurringTrigger(
        kind=sched.trigger_kind,
        cron_expr=sched.cron_expr,
        interval_seconds=sched.interval_seconds,
        timezone=sched.timezone or cluster_timezone,
        timeout_seconds=sched.timeout_seconds,
    )


def _matches(row: _StoredScheduleRow, sched: BuiltinSchedule, script_text: str, tz: str) -> bool:
    return (
        row.script == script_text
        and row.command == sched.command
        and row.trigger_kind == sched.trigger_kind
        and row.cron_expr == sched.cron_expr
        and row.interval_seconds == sched.interval_seconds
        and row.timezone == tz
        and row.timeout_seconds == sched.timeout_seconds
    )


def _create_schedule(
    conn: psycopg.Connection,
    sched: BuiltinSchedule,
    *,
    name: str,
    machine: str,
    script_text: str,
    trigger: RecurringTrigger,
    now: datetime,
) -> int:
    next_launch = (
        trigger.next_launch(now=now, cluster_timezone=trigger.timezone or "UTC")
        if sched.default_enabled
        else None
    )
    row = query_one(
        conn,
        IdRow,
        "INSERT INTO schedules "
        "(name, description, script, command, enabled, status, machine, trigger_kind, "
        "cron_expr, interval_seconds, timezone, timeout_seconds, on_owner_terminate, "
        "next_launch_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
        "'keep', %s) RETURNING id",
        (
            name,
            sched.description,
            script_text,
            sched.command,
            sched.default_enabled,
            "scheduled" if sched.default_enabled else "stopped",
            machine,
            sched.trigger_kind,
            sched.cron_expr,
            sched.interval_seconds,
            trigger.timezone,
            sched.timeout_seconds,
            next_launch,
        ),
    )
    if row is None:
        raise RuntimeError(f"failed to insert schedule {name}")
    conn.execute(
        "INSERT INTO schedule_versions (schedule_id, script, command, note) "
        "VALUES (%s, %s, %s, %s)",
        (row.id, script_text, sched.command, "initial"),
    )
    return row.id


def _resync_schedule(
    conn: psycopg.Connection,
    sched: BuiltinSchedule,
    row: _StoredScheduleRow,
    *,
    script_text: str,
    want: str,
    trigger: RecurringTrigger,
    now: datetime,
) -> bool:
    timezone = trigger.timezone or "UTC"
    if _matches(row, sched, script_text, timezone):
        return False
    next_launch = trigger.next_launch(now=now, cluster_timezone=timezone) if row.enabled else None
    conn.execute(
        "UPDATE schedules SET script = %s, command = %s, trigger_kind = %s, run_at = NULL, "
        "cron_expr = %s, interval_seconds = %s, timezone = %s, timeout_seconds = %s, "
        "on_owner_terminate = 'keep', desired_revision = desired_revision + 1, launch_count = 0, "
        "launch_generation = NULL, status = %s, last_error = NULL, next_launch_at = %s, "
        "updated_at = %s WHERE id = %s",
        (
            script_text,
            sched.command,
            sched.trigger_kind,
            sched.cron_expr,
            sched.interval_seconds,
            timezone,
            sched.timeout_seconds,
            "scheduled" if row.enabled else "stopped",
            next_launch,
            now,
            row.id,
        ),
    )
    if row.script != script_text or row.command != sched.command:
        conn.execute(
            "INSERT INTO schedule_versions (schedule_id, script, command, note) "
            "VALUES (%s, %s, %s, %s)",
            (row.id, script_text, sched.command, f"builtin-resync {want}"),
        )
    conn.execute(
        "INSERT INTO schedule_sync_requests (schedule_id, machine) VALUES (%s, %s) "
        "ON CONFLICT (schedule_id, machine) DO UPDATE SET requested_at = clock_timestamp()",
        (row.id, row.machine),
    )
    return True


def _provision_gateway_schedule(
    conn: psycopg.Connection,
    sched: BuiltinSchedule,
    *,
    script_text: str,
    want: str,
    machine: str,
    result: ProvisionResult,
    cluster_timezone: str,
    now: datetime,
) -> None:
    trigger = _resolved_trigger(sched, cluster_timezone)
    existing = query_one(
        conn,
        _StoredScheduleRow,
        "SELECT id, name, script, command, machine, enabled, trigger_kind, cron_expr, "
        "interval_seconds, timezone, timeout_seconds FROM schedules WHERE name = %s FOR UPDATE",
        (sched.name,),
    )
    if existing is None:
        _create_schedule(
            conn,
            sched,
            name=sched.name,
            machine=machine,
            script_text=script_text,
            trigger=trigger,
            now=now,
        )
        result.created.append(sched.name)
        return

    if _resync_schedule(
        conn, sched, existing, script_text=script_text, want=want, trigger=trigger, now=now
    ):
        result.resynced.append(sched.name)


def _provision_per_machine_schedule(
    conn: psycopg.Connection,
    sched: BuiltinSchedule,
    *,
    script_text: str,
    want: str,
    fallback_machine: str,
    result: ProvisionResult,
    cluster_timezone: str,
    now: datetime,
) -> None:
    trigger = _resolved_trigger(sched, cluster_timezone)
    targets = runner_machine_names(conn, fallback_machine=fallback_machine)
    for target_machine in targets:
        row_name = f"{sched.name}:{target_machine}"
        existing = query_one(
            conn,
            _StoredScheduleRow,
            "SELECT id, name, script, command, machine, enabled, trigger_kind, cron_expr, "
            "interval_seconds, timezone, timeout_seconds FROM schedules "
            "WHERE name = %s FOR UPDATE",
            (row_name,),
        )
        if existing is None:
            _create_schedule(
                conn,
                sched,
                name=row_name,
                machine=target_machine,
                script_text=script_text,
                trigger=trigger,
                now=now,
            )
            result.created.append(row_name)

    for drifted in query_all(
        conn,
        _StoredScheduleRow,
        "SELECT id, name, script, command, machine, enabled, trigger_kind, cron_expr, "
        "interval_seconds, timezone, timeout_seconds FROM schedules "
        "WHERE name LIKE %s AND owner_plugin IS NULL ORDER BY id FOR UPDATE",
        (f"{sched.name}:%",),
    ):
        if _resync_schedule(
            conn, sched, drifted, script_text=script_text, want=want, trigger=trigger, now=now
        ):
            result.resynced.append(drifted.name)


def provision_builtin_schedules(
    conn: psycopg.Connection,
    *,
    machine: str,
    cluster_timezone: str,
    now: datetime,
    path: Path | None = None,
) -> ProvisionResult:
    manifest = load_manifest(path)
    manifest_path = (path or MANIFEST_PATH).parent
    effective_now = now.astimezone(UTC)
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
                cluster_timezone=cluster_timezone,
                now=effective_now,
            )
        else:
            _provision_per_machine_schedule(
                conn,
                sched,
                script_text=script_text,
                want=want,
                fallback_machine=machine,
                result=result,
                cluster_timezone=cluster_timezone,
                now=effective_now,
            )
    return result
