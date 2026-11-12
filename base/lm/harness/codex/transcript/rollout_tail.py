from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from base.lm.harness.codex.reconciliation.rollout import rollout_path
from base.lm.harness.session.jsonl.position import (
    START,
    Position,
    RawRow,
    advance,
    row_digest,
    rows_after,
)
from base.lm.harness.session.jsonl.tail import end_position, next_chunk, verify_last_row

# meiva:note
# A thread's rollout file, read for the few lines the item history lacks, from a stored byte position and never
# from the start: `rows_after` is given a test on each line's bytes (`WANTED_MAIN` / `WANTED_CHILD`), so a line
# that is not one of those is passed over without being decoded (a rollout holds large lines of context the
# reader has no use for) and only moves the position. The lines wanted are a turn's end and abort, a sample's token
# usage and the report a finished child leaves in its parent; a child's file gives its token usage only. The file is
# found by the hint the server gave when the thread was opened, else by the thread id under the Codex home. Every
# read verifies that the last line read is still where it was (the same check Claude's file position makes), and a
# file that no longer holds it raises `PositionLostError`.
# meiva:link base/lm/harness/codex/docs/codex.md

WANTED_MAIN = (
    b'"task_complete"',
    b'"turn_aborted"',
    b'"token_usage_record"',
    b"<subagent_notification>",
)
WANTED_CHILD = (b'"token_usage_record"',)


@dataclass(frozen=True, slots=True)
class RolloutRead:
    rows: tuple[RawRow, ...]
    moved: Position
    bytes_read: int


@dataclass(frozen=True, slots=True)
class StartRead:
    position: Position
    bytes_read: int


class RolloutFile:
    def __init__(self, path: Path) -> None:
        self._path = path

    @classmethod
    def locate(cls, home: Path, thread: str, *, hint: str | None) -> RolloutFile | None:
        if hint is not None and Path(hint).is_file():
            return cls(Path(hint))
        found = rollout_path(home, thread)
        return None if found is None else cls(found)

    def stamp(self) -> tuple[int, int] | None:
        try:
            status = self._path.stat()
        except FileNotFoundError:
            return None
        return status.st_size, status.st_mtime_ns

    def read(self, position: Position, *, max_batch: int, max_row: int, main: bool) -> RolloutRead:
        needles = WANTED_MAIN if main else WANTED_CHILD
        with self._path.open("rb") as handle:
            read = verify_last_row(handle, position)
            chunk = next_chunk(handle, position, max_batch=max_batch, max_row=max_row)
        read += chunk.bytes_read
        rows = (
            *rows_after(position, chunk.data, lambda line: any(n in line for n in needles)),
            *((chunk.skipped,) if chunk.skipped else ()),
        )
        if not rows:
            return RolloutRead((), position, read)
        return RolloutRead(rows, advance(position, rows, chunk.data), read)

    # meiva:note
    # Where to start reading a rollout whose thread already holds history: just before the line that names the
    # first client id we sent (the user message that opened our first turn), looked for in the last `SCAN_BYTES` of
    # the file; at the end of the file's last complete line when no id is given or none is found there.
    def start_before(self, wire_ids: tuple[str, ...], *, max_row: int) -> StartRead:
        with self._path.open("rb") as handle:
            if wire_ids:
                found = _before_needle(
                    handle, tuple(f'"client_id":"{w}"'.encode() for w in wire_ids)
                )
                if found is not None:
                    return found
            end, read = end_position(handle, max_row=max_row)
        return StartRead(end or START, read)


SCAN_BYTES = 8 * 1024 * 1024


def _before_needle(handle: BinaryIO, needles: tuple[bytes, ...]) -> StartRead | None:
    size = handle.seek(0, 2)
    base = max(size - SCAN_BYTES, 0)
    _ = handle.seek(base)
    window = handle.read()
    hits = [at for needle in needles if (at := window.find(needle)) >= 0]
    if not hits:
        return None
    line_start = window.rfind(b"\n", 0, min(hits)) + 1
    if line_start == 0 and base > 0:
        return None
    if line_start == 0:
        return StartRead(START, len(window))
    previous_start = window.rfind(b"\n", 0, line_start - 1) + 1
    if previous_start == 0 and base > 0:
        return None
    previous = window[previous_start:line_start]
    return StartRead(
        Position(
            start=base + previous_start,
            offset=base + line_start,
            lines=1,
            sha256=row_digest(previous),
        ),
        len(window),
    )
