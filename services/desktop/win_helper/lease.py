from __future__ import annotations

import json
import threading
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .constants import LEASE_DEFAULT_SECONDS, LEASE_MAX_SECONDS, LEASE_MIN_SECONDS
from .win32_ffi import is_object_dict, object_of


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _parse_iso(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


class DesktopLeaseManager:
    def __init__(self, path: Path, *, now: Callable[[], datetime]) -> None:
        self._path = path
        self._now = now
        self._lock = threading.Lock()

    def _read_raw(self) -> dict[str, object] | None:
        if not self._path.is_file():
            return None
        try:
            data = object_of(json.loads, self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return dict(data) if is_object_dict(data) else None

    def _write_raw(self, data: dict[str, object]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        _ = tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        _ = tmp.replace(self._path)

    def _clear_file(self) -> None:
        with suppress(OSError):
            if self._path.is_file():
                self._path.unlink()

    def _active_locked(self) -> dict[str, object] | None:
        data = self._read_raw()
        if not data:
            return None
        exp = _parse_iso(data.get("expires_at"))
        current = self._now().astimezone(UTC)
        if exp is None or exp <= current:
            self._clear_file()
            return None
        remaining = max(0, int((exp - current).total_seconds()))
        out = dict(data)
        out["remaining_seconds"] = remaining
        return out

    def status(self) -> dict[str, object] | None:
        with self._lock:
            return self._active_locked()

    def acquire(
        self,
        holder: str,
        purpose: str = "",
        ttl_seconds: int = LEASE_DEFAULT_SECONDS,
        *,
        force: bool = False,
    ) -> dict[str, object]:
        holder = holder.strip()
        if not holder:
            return {"ok": False, "code": "INVALID_HOLDER", "message": "holder is required"}
        ttl = max(LEASE_MIN_SECONDS, min(LEASE_MAX_SECONDS, int(ttl_seconds)))
        with self._lock:
            active = self._active_locked()
            if active and active.get("holder") != holder and not force:
                return {
                    "ok": False,
                    "code": "DESKTOP_LEASE_HELD",
                    "lease": active,
                    "message": (
                        f"Desktop lease is currently held by '{active.get('holder')}' "
                        f"({active.get('purpose') or 'no purpose'}), "
                        f"{active.get('remaining_seconds')}s remaining."
                    ),
                }
            now = self._now().astimezone(UTC)
            record: dict[str, object] = {
                "holder": holder,
                "purpose": purpose.strip(),
                "acquired_at": active["acquired_at"]
                if (active and active.get("holder") == holder and "acquired_at" in active)
                else _iso(now),
                "heartbeat_at": _iso(now),
                "expires_at": _iso(now + timedelta(seconds=ttl)),
                "ttl_seconds": ttl,
            }
            self._write_raw(record)
            record["remaining_seconds"] = ttl
            return {"ok": True, "lease": record}

    def renew(self, holder: str, ttl_seconds: int = LEASE_DEFAULT_SECONDS) -> dict[str, object]:
        holder = holder.strip()
        if not holder:
            return {"ok": False, "code": "INVALID_HOLDER", "message": "holder is required"}
        ttl = max(LEASE_MIN_SECONDS, min(LEASE_MAX_SECONDS, int(ttl_seconds)))
        with self._lock:
            active = self._active_locked()
            if not active:
                return {
                    "ok": False,
                    "code": "NO_ACTIVE_LEASE",
                    "message": "No active lease to renew",
                }
            if active.get("holder") != holder:
                return {
                    "ok": False,
                    "code": "DESKTOP_LEASE_HELD",
                    "lease": active,
                    "message": f"Lease is held by '{active.get('holder')}', not '{holder}'.",
                }
            now = self._now().astimezone(UTC)
            record: dict[str, object] = {
                "holder": holder,
                "purpose": active.get("purpose", ""),
                "acquired_at": active.get("acquired_at", _iso(now)),
                "heartbeat_at": _iso(now),
                "expires_at": _iso(now + timedelta(seconds=ttl)),
                "ttl_seconds": ttl,
            }
            self._write_raw(record)
            record["remaining_seconds"] = ttl
            return {"ok": True, "lease": record}

    def release(self, holder: str = "", *, force: bool = False) -> dict[str, object]:
        holder = holder.strip()
        with self._lock:
            active = self._active_locked()
            if not active:
                return {"ok": True, "released": False, "message": "No active lease"}
            if not force and holder and active.get("holder") != holder:
                return {
                    "ok": False,
                    "code": "DESKTOP_LEASE_HELD",
                    "lease": active,
                    "message": (
                        f"Cannot release lease held by '{active.get('holder')}' as '{holder}'."
                    ),
                }
            if not force and not holder:
                return {
                    "ok": False,
                    "code": "INVALID_HOLDER",
                    "lease": active,
                    "message": "Specify holder (or force=True) to release active lease.",
                }
            self._clear_file()
            return {"ok": True, "released": True, "previous_holder": active.get("holder")}

    def check_mutating_allowed(self, holder: str = "") -> dict[str, object] | None:
        holder = holder.strip()
        with self._lock:
            active = self._active_locked()
            if not active:
                return None
            if holder and active.get("holder") == holder:
                now = self._now().astimezone(UTC)
                updated = dict(active)
                updated.pop("remaining_seconds", None)
                updated["heartbeat_at"] = _iso(now)
                self._write_raw(updated)
                return None
            return {
                "ok": False,
                "code": "DESKTOP_LEASE_HELD",
                "lease": active,
                "message": (
                    f"Desktop lease is held by '{active.get('holder')}' "
                    f"(purpose: {active.get('purpose') or 'unspecified'}, "
                    f"{active.get('remaining_seconds')}s remaining). "
                    f"Pass matching holder='{active.get('holder')}' or wait until released."
                ),
            }
