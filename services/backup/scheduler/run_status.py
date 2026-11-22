# meiva:note
# Durable last-run outcome markers for backup dumps and recovery drills. They survive scheduler
# restarts so Prometheus state gauges reflect the real last run outcome without false-clearing a
# failure or false-alarming on fresh installs; newer durable artifacts supersede an older failure.

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeGuard

from base.host.boundary.untyped import object_of
from base.host.private_storage import write_private_bytes
from services.backup.dump import newest_dump_instant
from services.backup.layout import BackupTree
from services.backup.scheduler.recovery_drill import load_local_dump_restore_success


@dataclass(frozen=True)
class RunStatus:
    ok: bool
    recorded_at: datetime


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def load_last_run_status(marker: Path) -> RunStatus | None:
    if not marker.exists():
        return None
    try:
        parsed = object_of(json.loads, marker.read_text(encoding="utf-8"))
        if not _is_object_dict(parsed) or set(parsed) != {"ok", "recorded_at"}:
            return None
        ok = parsed["ok"]
        recorded_at = parsed["recorded_at"]
        if not isinstance(ok, bool) or not isinstance(recorded_at, str):
            return None
        stamp = datetime.fromisoformat(recorded_at)
        if stamp.tzinfo is None:
            return None
        return RunStatus(ok=ok, recorded_at=stamp.astimezone(UTC))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def record_last_run_status(marker: Path, *, ok: bool, now: datetime) -> None:
    if now.tzinfo is None:
        raise ValueError("backup run status time must be TZ-aware")
    payload = json.dumps(
        {"ok": ok, "recorded_at": now.astimezone(UTC).isoformat()},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    write_private_bytes(marker, payload)


def resolve_dump_last_run_ok(tree: BackupTree) -> bool:
    status = load_last_run_status(tree.dump_status_marker)
    if status is None or status.ok:
        return True
    newest = newest_dump_instant(tree.managed)
    return newest is not None and newest > status.recorded_at


def resolve_restore_drill_last_run_ok(tree: BackupTree) -> bool:
    try:
        last_success = load_local_dump_restore_success(tree.restore_marker)
    except RuntimeError:
        return False
    status = load_last_run_status(tree.restore_status_marker)
    if status is None or status.ok:
        return True
    return last_success is not None and last_success > status.recorded_at
