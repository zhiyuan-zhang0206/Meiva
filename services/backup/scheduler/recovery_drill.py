"""Durable cadence for the weekly local logical-backup restore drill."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeGuard

from base.host.private_storage import write_private_bytes


def load_local_dump_restore_success(marker: Path) -> datetime | None:
    """Read the one durable weekly-drill success marker, if one exists."""
    if not marker.exists():
        return None
    try:
        return _parse_success_marker(marker.read_text())
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("logical restore drill success marker is invalid") from exc


def record_local_dump_restore_success(now: datetime, marker: Path) -> None:
    """Atomically publish a completed weekly-drill marker after verification."""
    if now.tzinfo is None:
        raise ValueError("recovery drill success time must be TZ-aware")
    payload = json.dumps(
        {"completed_at": now.astimezone(UTC).isoformat()}, sort_keys=True, separators=(",", ":")
    ).encode()
    write_private_bytes(marker, payload)


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def _parse_success_marker(value: str) -> datetime:
    parsed: object = json.loads(value)
    if not _is_object_dict(parsed):
        raise TypeError("invalid marker shape")
    if set(parsed) != {"completed_at"}:
        raise ValueError("invalid marker shape")
    completed_at = parsed["completed_at"]
    if not isinstance(completed_at, str):
        raise TypeError("invalid marker shape")
    stamp = datetime.fromisoformat(completed_at)
    if stamp.tzinfo is None:
        raise ValueError("marker time is naive")
    return stamp.astimezone(UTC)
