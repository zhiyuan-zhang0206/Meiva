from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from base.host.atomic_io import write_text_atomic
from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of
from base.lm.harness.antigravity.timing import timing_key
from base.lm.harness.antigravity.trajectory import Trajectory
from base.log import logger

MAX_SIDECAR_BYTES = 8 * 1024 * 1024
_MAX_ROW_BYTES = 16 * 1024 * 1024


class Measurement(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    ms: int | None = Field(ge=0, le=9_007_199_254_740_991)


class TimingRecord(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    version: Literal[1] = 1
    total: int = Field(ge=0)
    device: int
    inode: int
    size: int = Field(ge=0)
    modified_ns: int
    native_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    entries: dict[Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)$")], Measurement]


def timing_path(record: Path) -> Path:
    return record.with_name("meiva-thinking-timing.json")


def load_timings(record: Path, *, allow_growth: bool = False) -> TimingRecord | None:
    try:
        with timing_path(record).open("rb") as handle:
            raw = handle.read(MAX_SIDECAR_BYTES + 1)
        if len(raw) > MAX_SIDECAR_BYTES:
            _oversized()
        data = TimingRecord.model_validate(
            object_of(json.loads, raw, object_pairs_hook=_unique_object)
        )
        stat = record.stat()
        if (data.device, data.inode) != (stat.st_dev, stat.st_ino) or stat.st_size < data.size:
            return None
        if not allow_growth and (stat.st_size != data.size or stat.st_mtime_ns != data.modified_ns):
            return None
        if len({value.fingerprint for value in data.entries.values()}) != len(data.entries):
            raise_invalid_identity()
        return data
    except FileNotFoundError:
        return None
    except (OSError, ValueError, ValidationError) as exc:
        logger.warning(
            "native timing sidecar is unreadable; record timing remains unknown ({})",
            type(exc).__name__,
            event="harness_timing_unreadable",
        )
        return None


# meiva:note
# A flock serializes collectors, atomic replacement publishes one complete revision, and a smaller
# native snapshot never replaces a newer one. Only digests, byte offsets and measured milliseconds
# are persisted. Duplicate row identities invalidate timing instead of selecting a convenient row.
# meiva:link base/lm/harness/antigravity/docs/antigravity.md
def store_timings(record: Path, trajectory: Trajectory) -> None:
    try:
        with timing_path(record).with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            _store(record, trajectory)
    except (OSError, ValueError) as exc:
        logger.warning(
            "native timing could not be collected; provider execution is unchanged ({})",
            type(exc).__name__,
            event="harness_timing_collection_failed",
        )


def _store(record: Path, trajectory: Trajectory) -> None:
    old = load_timings(record, allow_growth=True)
    native = _native_facts(trajectory)
    invalid = [step.index for step in trajectory.steps if step.invalid_thinking_duration]
    native_hash = hashlib.sha256(json.dumps([native, invalid], sort_keys=True).encode()).hexdigest()
    stat = record.stat()
    stale = old is not None and trajectory.num_steps < old.total
    if _unchanged(old, stat, native_hash, trajectory.num_steps):
        return
    if invalid and not stale:
        logger.warning(
            "native timing collection: invalid terminal thinkingDuration",
            event="harness_timing_collection_failed",
        )
    if stale and old is not None:
        native = {}
        native_hash = old.native_hash
    found = row_identities(record)
    entries, ambiguous = _merge_measurements(old, found, native)
    if ambiguous:
        logger.warning(
            "native reasoning timing has {count} ambiguous record identities",
            count=ambiguous,
            event="harness_timing_ambiguous",
        )
    after = record.stat()
    if (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        return
    data = TimingRecord(
        total=max(trajectory.num_steps, 0 if old is None else old.total),
        native_hash=native_hash,
        modified_ns=stat.st_mtime_ns,
        device=stat.st_dev,
        inode=stat.st_ino,
        size=stat.st_size,
        entries=entries,
    )
    encoded = data.model_dump_json()
    if len(encoded.encode()) > MAX_SIDECAR_BYTES:
        raise ValueError("timing sidecar exceeds its byte limit")
    if old != data:
        write_text_atomic(timing_path(record), encoded, mode=0o600)


def _oversized() -> None:
    raise ValueError("timing sidecar exceeds its byte limit")


def row_identities(record: Path) -> dict[str, int | None]:
    found: dict[str, int | None] = {}
    with record.open("rb") as handle:
        while True:
            offset = handle.tell()
            line = handle.readline(_MAX_ROW_BYTES + 1)
            if not line:
                break
            if len(line) > _MAX_ROW_BYTES:
                raise ValueError("native record row exceeds timing collector limit")
            if not line.endswith(b"\n"):
                break
            row = object_of(json.loads, line)
            if not is_object_dict(row) or row.get("type") != "PLANNER_RESPONSE":
                continue
            key = timing_key(row.get("step_index"), row.get("created_at"), row.get("thinking"))
            if key is not None:
                found[key] = None if key in found else offset
    return found


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise_invalid_identity()
        result[key] = value
    return result


def raise_invalid_identity() -> None:
    raise ValueError("timing sidecar has duplicate identities")


def _native_facts(trajectory: Trajectory) -> dict[str, int]:
    return {
        step.timing_key: step.thinking_ms
        for step in trajectory.steps
        if step.timing_key is not None and step.thinking_ms is not None
    }


def _unchanged(
    old: TimingRecord | None, stat: os.stat_result, native_hash: str, total: int
) -> bool:
    return (
        old is not None
        and old.size == stat.st_size
        and old.modified_ns == stat.st_mtime_ns
        and (total < old.total or (old.native_hash == native_hash and old.total == total))
    )


def _merge_measurements(
    old: TimingRecord | None,
    found: dict[str, int | None],
    native: dict[str, int],
) -> tuple[dict[str, Measurement], int]:
    entries = (
        {}
        if old is None
        else {
            pos: value
            for pos, value in old.entries.items()
            if found.get(value.fingerprint) == int(pos)
        }
    )
    ambiguous = sum(offset is None for offset in found.values())
    for key, offset in found.items():
        if offset is None or key not in native:
            continue
        value = Measurement(fingerprint=key, ms=native[key])
        previous = entries.get(str(offset))
        if previous is not None and previous.fingerprint == key and previous.ms != value.ms:
            ambiguous += 1
            value = Measurement(fingerprint=key, ms=None)
        entries[str(offset)] = value
    return entries, ambiguous
