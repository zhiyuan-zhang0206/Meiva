"""Generation-owned admission freeze for new PTY sessions.

The host runs one cluster, so the marker and its lock live in its ``$MEIVA_HOME``
(``pty-allocation-freeze.json`` and ``pty-allocation.lock``). The allocation gate
affects only absent session -> new PTY session; desired-state reconcilers reap
existing exact sessions that belong to an earlier generation.

The marker is an operator capability: only its exact random generation may
resume allocation. Resuming leaves that UUID as the current session generation,
so desired-state reconcilers can reject records from the preceding flip.
Readers classify malformed state as ``invalid`` so the allocation path fails
closed instead of guessing that a corrupt marker means unfrozen.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import random
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Never, TypeGuard

from base.clock import Clock
from base.host.atomic_io import AtomicWriter
from base.host.boundary.untyped import object_of
from base.native_process.os_platform import file_lock

SCHEMA_VERSION = 1
_LOCK_TIMEOUT_S = 60.0
_STATE_FILENAME = "pty-allocation-freeze.json"
_LOCK_FILENAME = "pty-allocation.lock"

_log = logging.getLogger("base.sessions.pty.allocation_freeze")


@dataclass(frozen=True)
class PtyAllocationFreeze:
    """Complete projection of the host allocation marker."""

    status: Literal["inactive", "frozen", "invalid"]
    generation: str | None = None
    holder: str | None = None
    reason: str | None = None
    created_at: dt.datetime | None = None
    error: str | None = None


class PtyAllocationAlreadyFrozenError(RuntimeError):
    """A valid generation already owns the allocation freeze."""


class InvalidPtyAllocationFreezeError(RuntimeError):
    """The marker is corrupt, so allocation remains frozen fail-closed."""


def _invalid(message: str) -> Never:
    raise ValueError(message)


def state_path(home: Path) -> Path:
    return home / _STATE_FILENAME


def lock_path(home: Path) -> Path:
    return home / _LOCK_FILENAME


def _timestamp(value: object) -> dt.datetime:
    if not isinstance(value, str):
        _invalid("created_at must be an RFC3339 string")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        _invalid("created_at must carry a timezone")
    return parsed.astimezone(dt.UTC)


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def _parse(raw_value: object) -> PtyAllocationFreeze:
    if not _is_object_dict(raw_value):
        _invalid("marker root must be an object")
    raw = raw_value
    if raw.get("schema_version") != SCHEMA_VERSION:
        _invalid(f"unknown schema_version {raw.get('schema_version')!r}")
    state = raw.get("state")
    if state not in ("active", "frozen"):
        _invalid(f"unknown state {state!r}")
    generation = raw.get("generation")
    holder = raw.get("holder")
    reason = raw.get("reason")
    if not isinstance(generation, str) or not generation:
        _invalid("generation must be a non-empty string")
    if not isinstance(holder, str) or not holder:
        _invalid("holder must be a non-empty string")
    if not isinstance(reason, str) or not reason:
        _invalid("reason must be a non-empty string")
    return PtyAllocationFreeze(
        status="frozen" if state == "frozen" else "inactive",
        generation=generation,
        holder=holder,
        reason=reason,
        created_at=_timestamp(raw.get("created_at")),
    )


def _read_unlocked(path: Path) -> PtyAllocationFreeze:
    try:
        raw = object_of(json.loads, path.read_text())
    except FileNotFoundError:
        return PtyAllocationFreeze(status="inactive")
    except (OSError, ValueError) as exc:
        _log.warning("[pty-allocation] cannot read %s: %s", path, exc)
        return PtyAllocationFreeze(status="invalid", error=f"marker unreadable: {exc}")
    try:
        return _parse(raw)
    except (TypeError, ValueError) as exc:
        _log.warning("[pty-allocation] invalid %s: %s", path, exc)
        return PtyAllocationFreeze(status="invalid", error=str(exc))


def _from_dict(res: dict[str, object]) -> PtyAllocationFreeze:
    raw_status = res.get("status")
    status: Literal["inactive", "frozen", "invalid"] = (
        raw_status if raw_status in ("inactive", "frozen", "invalid") else "inactive"
    )
    created = res.get("created_at")
    created_at = None
    if isinstance(created, str):
        created_at = _timestamp(created)
    return PtyAllocationFreeze(
        status=status,
        generation=str(res["generation"]) if res.get("generation") else None,
        holder=str(res["holder"]) if res.get("holder") else None,
        reason=str(res["reason"]) if res.get("reason") else None,
        created_at=created_at,
        error=str(res["error"]) if res.get("error") else None,
    )


# meiva:note
# Read one atomic marker snapshot; in-memory in pty-sessions with file fallback for invalid markers.
def read(*, home: Path) -> PtyAllocationFreeze:
    file = state_path(home)
    if file.exists():
        return _read_unlocked(file)

    from base.paths import run_dir
    from base.sessions.pty import client
    from base.sessions.pty.paths import service_socket_path

    socket_path = service_socket_path(run_dir(home))
    try:
        res = client.request("freeze_status", socket_path=socket_path)
        return _from_dict(res)
    except (client.ServiceDownError, FileNotFoundError):
        return PtyAllocationFreeze(status="inactive")


def current_generation(*, home: Path) -> str | None:
    """The current or pending flip generation, failing closed when corrupt."""
    snapshot = read(home=home)
    if snapshot.status == "invalid":
        raise InvalidPtyAllocationFreezeError(
            f"invalid marker {state_path(home)}: {snapshot.error}"
        )
    return snapshot.generation


def write_atomic(path: Path, payload: dict[str, object], *, writer: AtomicWriter) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer.write_text(
        path,
        json.dumps(payload, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
        mode=0o600,
        prefix=".pty-allocation-freeze-",
    )
    try:
        writer.fsync_parent(path)
    except OSError:
        # The replace already committed visible state. Do not report that the
        # caller failed to freeze when every new-session path now sees frozen.
        _log.warning("[pty-allocation] directory fsync failed after marker commit", exc_info=True)


@contextlib.contextmanager
def locked_freeze_state(*, home: Path) -> Generator[PtyAllocationFreeze]:
    """Hold the host allocation mutex and yield its current marker state.

    The pty-sessions service's ``new`` keeps this lock until the session is registered. Therefore, after
    ``freeze`` returns, every earlier allocation is fully visible and every
    later absent-to-live transition observes the marker and is refused.
    """
    with file_lock(lock_path(home), timeout_s=_LOCK_TIMEOUT_S):
        yield read(home=home)


# meiva:note
# Create a new operator-owned allocation freeze generation in pty-sessions memory.
def freeze(
    *,
    home: Path,
    holder: str,
    reason: str,
    writer: AtomicWriter | None = None,
    clock: Clock,
    rng: random.Random,
) -> PtyAllocationFreeze:
    del writer, clock, rng
    if not holder:
        raise ValueError("holder must be non-empty")
    if not reason:
        raise ValueError("reason must be non-empty")

    from base.paths import run_dir
    from base.sessions.pty import client
    from base.sessions.pty.paths import service_socket_path

    socket_path = service_socket_path(run_dir(home))
    try:
        res = client.request("freeze", socket_path=socket_path, holder=holder, reason=reason)
        return _from_dict(res)
    except client.ServiceError as exc:
        if "already active" in str(exc):
            raise PtyAllocationAlreadyFrozenError(str(exc)) from exc
        raise


def resume(generation: str, *, home: Path, writer: AtomicWriter | None = None) -> bool:
    """CAS-activate exactly ``generation``; never clear a replacement owner."""
    del writer
    if not generation:
        raise ValueError("generation must be non-empty")

    from base.paths import run_dir
    from base.sessions.pty import client
    from base.sessions.pty.paths import service_socket_path

    socket_path = service_socket_path(run_dir(home))
    try:
        res = client.request("resume", socket_path=socket_path, generation=generation)
        return bool(res.get("resumed", False))
    except (client.ServiceDownError, FileNotFoundError):
        return False
