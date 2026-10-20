# meiva:note
# The process each unit's current generation is — written by the root that made it.
#
# Root writes one record per unit right after it spawns that unit's process, and
# clears it when that process exits. A record therefore outlives a root that left
# its children behind: a crash, or the code-version gate's hard exit, which runs
# on a pool worker thread and cannot stop an asynchronous tree (task #5474). The
# next root reads the record before it starts the same unit: a recorded birth
# that is still live is the generation it must stop first, because that process
# still holds the unit's port, socket or instance lock. Only a recorded birth may
# be signalled; a live process no record describes is reported, never killed.
# meiva:link services/supervision/meiva_root/docs/meiva_root.meiva.okf.md

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from base.native_process.ownership import OwnedProcess

_log = logging.getLogger(__name__)

_DIRECTORY = "units"
_RECORD_FIELDS = frozenset(
    {"pid", "birth", "starttime", "generation", "manifest_digest", "started_at"}
)


# meiva:note
# One unit's spawned process: its native birth and the generation that made it.
@dataclass(frozen=True, slots=True)
class GenerationRecord:
    identity: OwnedProcess
    generation: int
    manifest_digest: str
    started_at: float


# meiva:note
# This unit's record file under `run_dir`.
def record_path(run_dir: Path, unit_id: str) -> Path:
    return run_dir / _DIRECTORY / f"{unit_id}.json"


# meiva:note
# Persist one unit's spawn record; a lost record only costs a stale-port retry,
# so an unwritable run directory logs and never fails the spawn that succeeded.
def write(run_dir: Path, unit_id: str, record: GenerationRecord) -> None:
    from base.host.atomic_io import write_text_atomic

    body = json.dumps(
        {
            "pid": record.identity.pid,
            "birth": record.identity.birth,
            "starttime": record.identity.starttime,
            "generation": record.generation,
            "manifest_digest": record.manifest_digest,
            "started_at": record.started_at,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    path = record_path(run_dir, unit_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, body, mode=0o600, sync_parent=True)
    except OSError:
        _log.exception("unit %s: could not record its spawned generation", unit_id)


# meiva:note
# The recorded spawn of `unit_id`, or None when absent or unreadable.
#
# An unreadable record is dropped with a log line: it can only be a leftover of a
# half-written file, and refusing to start the whole unit over it would turn a
# cosmetic loss into an outage.
def read(run_dir: Path, unit_id: str) -> GenerationRecord | None:
    path = record_path(run_dir, unit_id)
    try:
        raw: object = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning("unit %s: discarding unreadable spawn record %s (%s)", unit_id, path, exc)
        clear(run_dir, unit_id)
        return None
    if not isinstance(raw, dict):
        _log.warning("unit %s: discarding a spawn record with an unexpected shape", unit_id)
        clear(run_dir, unit_id)
        return None
    fields = cast("dict[str, object]", raw)
    if set(fields) != set(_RECORD_FIELDS):
        _log.warning("unit %s: discarding a spawn record with an unexpected shape", unit_id)
        clear(run_dir, unit_id)
        return None
    try:
        identity = OwnedProcess(
            pid=_as_int(fields["pid"], "pid"),
            birth=_as_float(fields["birth"], "birth"),
            starttime=(
                None if fields["starttime"] is None else _as_int(fields["starttime"], "starttime")
            ),
        )
        return GenerationRecord(
            identity=identity,
            generation=_as_int(fields["generation"], "generation"),
            manifest_digest=_as_str(fields["manifest_digest"]),
            started_at=_as_float(fields["started_at"], "started_at"),
        )
    except (TypeError, ValueError) as exc:
        _log.warning("unit %s: discarding a malformed spawn record (%s)", unit_id, exc)
        clear(run_dir, unit_id)
        return None


# meiva:note
# Drop the record — of this unit, or only of `generation` when it names one.
#
# The generation guard keeps a slow exit from erasing the record of the
# replacement that already replaced it.
def clear(run_dir: Path, unit_id: str, *, generation: int | None = None) -> None:
    path = record_path(run_dir, unit_id)
    if generation is not None:
        current = read(run_dir, unit_id)
        if current is not None and current.generation != generation:
            return
    path.unlink(missing_ok=True)


def _as_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} is not an integer")
    return value


def _as_float(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field} is not a number")
    return float(value)


def _as_str(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("manifest_digest is not a string")
    return value
