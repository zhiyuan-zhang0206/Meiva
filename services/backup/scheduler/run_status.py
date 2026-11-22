# meiva:note
# Durable last-run outcome markers for backup dumps and recovery drills. They survive scheduler
# restarts so Prometheus state gauges reflect the real last run outcome without false-clearing a
# failure or false-alarming on fresh installs; newer durable artifacts supersede an older failure,
# while unreadable or corrupt markers fail closed (False) and log a warning rather than masking an
# error as healthy.

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeGuard

from base.host.boundary.untyped import object_of
from base.host.private_storage import write_private_bytes
from services.backup.dump import newest_dump_instant
from services.backup.layout import BackupTree
from services.backup.scheduler.recovery_drill import load_local_dump_restore_success

_log = logging.getLogger(__name__)


class RunStatusError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunStatus:
    ok: bool
    recorded_at: datetime


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def _parse_run_status(raw: str) -> RunStatus:
    parsed = object_of(json.loads, raw)
    if not _is_object_dict(parsed) or set(parsed) != {"ok", "recorded_at"}:
        raise ValueError("invalid run status marker shape")
    ok = parsed["ok"]
    recorded_at = parsed["recorded_at"]
    if not isinstance(ok, bool) or not isinstance(recorded_at, str):
        raise TypeError("invalid run status marker field types")
    stamp = datetime.fromisoformat(recorded_at)
    if stamp.tzinfo is None:
        raise ValueError("run status marker time is naive")
    return RunStatus(ok=ok, recorded_at=stamp.astimezone(UTC))


def load_last_run_status(marker: Path) -> RunStatus | None:
    try:
        raw = marker.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RunStatusError(f"backup run status marker {marker} is unreadable") from exc
    try:
        return _parse_run_status(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RunStatusError(f"backup run status marker {marker} is invalid") from exc


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
    try:
        status = load_last_run_status(tree.dump_status_marker)
    except RunStatusError:
        _log.warning(
            "[pg-backup] failed reading dump run status marker %s",
            tree.dump_status_marker,
            exc_info=True,
        )
        return False
    if status is None or status.ok:
        return True
    try:
        newest = newest_dump_instant(tree.managed)
    except OSError:
        _log.warning(
            "[pg-backup] failed scanning managed dumps under %s",
            tree.managed,
            exc_info=True,
        )
        return False
    return newest is not None and newest > status.recorded_at


def resolve_restore_drill_last_run_ok(tree: BackupTree) -> bool:
    try:
        last_success = load_local_dump_restore_success(tree.restore_marker)
    except RuntimeError:
        _log.warning(
            "[pg-backup] failed reading restore drill success marker %s",
            tree.restore_marker,
            exc_info=True,
        )
        return False
    try:
        status = load_last_run_status(tree.restore_status_marker)
    except RunStatusError:
        _log.warning(
            "[pg-backup] failed reading restore drill status marker %s",
            tree.restore_status_marker,
            exc_info=True,
        )
        return False
    if status is None or status.ok:
        return True
    return last_success is not None and last_success > status.recorded_at
