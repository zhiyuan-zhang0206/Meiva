from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of
from base.lm.harness.session.transcript.record_file import BLOCK_BYTES, RecordFile
from base.paths import harness_sessions_dir


def row_uuid(row: Mapping[str, object]) -> str | None:
    attachment = row.get("attachment")
    if is_object_dict(attachment):
        source = attachment.get("source_uuid")
        if isinstance(source, str) and source:
            return source
    value = row.get("uuid")
    return value if isinstance(value, str) and value else None


def is_child_conversation_row(row: Mapping[str, object]) -> bool:
    return row.get("type") in ("user", "assistant")


def find_earliest_input_offset(
    file: RecordFile, target_uuids: set[str], primary_uuid: str | None
) -> int | None:
    earliest: int | None = None
    pos = file.complete_end()
    while pos > 0:
        block = file.rows_before(pos, BLOCK_BYTES)
        found_primary = False
        for row in block.rows:
            uid = row_uuid(row.row)
            if uid in target_uuids:
                if earliest is None or row.offset < earliest:
                    earliest = row.offset
                if primary_uuid is not None and uid == primary_uuid:
                    found_primary = True
        if found_primary or block.start == 0 or block.start >= pos:
            break
        pos = block.start
    return earliest


def read_row_uuids(path: Path) -> set[str]:
    uuids: set[str] = set()
    try:
        with path.open("rb") as handle:
            for raw_line in handle:
                try:
                    parsed = object_of(json.loads, raw_line.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if is_object_dict(parsed) and (uid := row_uuid(parsed)) is not None:
                    uuids.add(uid)
    except OSError:
        return set()
    return uuids


def parent_fork_cut(file: RecordFile, parent_uuids: frozenset[str]) -> tuple[int | None, bool]:
    saw_child = False
    pos = file.complete_end()
    while pos > 0:
        block = file.rows_before(pos, BLOCK_BYTES)
        for row in reversed(block.rows):
            uid = row_uuid(row.row)
            if uid is not None and uid in parent_uuids:
                return row.end, saw_child
            if is_child_conversation_row(row.row):
                saw_child = True
        if block.start == 0 or block.start >= pos:
            break
        pos = block.start
    return None, saw_child


def fork_floor_path(home: Path, agent_id: int, harness: str) -> Path:
    return harness_sessions_dir(home=home) / f"{agent_id}.{harness}.floor"


def read_fork_floor(path: Path, *, session_id: str) -> int | None:
    try:
        raw = object_of(json.loads, path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not is_object_dict(raw) or raw.get("session_id") != session_id:
        return None
    floor = raw.get("floor")
    if isinstance(floor, int) and not isinstance(floor, bool) and floor >= 0:
        return floor
    return None


def write_fork_floor(path: Path, *, session_id: str, floor: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".floor-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"session_id": session_id, "floor": floor}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        Path(temporary).replace(path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
