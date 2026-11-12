from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from base.lm.harness.session.transcript.position import Position, rows_after
from base.lm.harness.session.transcript.scrub import scrub

# meiva:note
# A harness CLI's own record file read in bounded pieces, whatever the CLI is (Claude Code's transcript and Codex's
# rollout are both JSON lines that only grow). A row's identity is its byte offset in the file (the file only grows,
# so an offset is stable, ordered and the same however the row was reached). Reads go backwards from a line boundary,
# one block at a time, so what a request reads follows the size of the window it asks for and not the size of the
# file, and `bytes_read` counts what was read so a test or a gauge can state what a request cost. A row that cannot be
# decoded is skipped: the file is the CLI's own and its rows are never repaired here. What a row means and where the
# file lives are each harness's own (`claude/transcript/window.py`, `codex/transcript/window.py`).
# meiva:link base/lm/harness/docs/transcript-projection.md

BLOCK_BYTES = 256 * 1024
MAX_BLOCK_BYTES = 16 * 1024 * 1024
_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class FileRow:
    offset: int
    end: int
    row: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class Block:
    start: int
    end: int
    rows: tuple[FileRow, ...]


class RecordFile:
    def __init__(self, path: Path) -> None:
        self._path = path
        self.bytes_read = 0

    def size(self) -> int:
        return self._path.stat().st_size

    # meiva:note
    # The end of the last complete line: a row the CLI is still writing is not read.
    def complete_end(self) -> int:
        with self._path.open("rb") as handle:
            position = handle.seek(0, 2)
            while position > 0:
                low = max(0, position - _CHUNK_BYTES)
                _ = handle.seek(low)
                data = handle.read(position - low)
                self.bytes_read += len(data)
                cut = data.rfind(b"\n")
                if cut >= 0:
                    return low + cut + 1
                position = low
        return 0

    # meiva:note
    # The rows in the block of about `want` bytes that ends at `end` (a line boundary): the block starts at the
    # first line boundary inside it, widened while one row is longer than the block. `start == 0` means
    # the file's first row is in it.
    def rows_before(self, end: int, want: int = BLOCK_BYTES) -> Block:
        low = max(0, end - want)
        while True:
            data = self._read(max(0, low - 1), end)
            first = max(0, low - 1)
            if low == 0:
                start = 0
            elif data[:1] == b"\n":
                data, start = data[1:], low
            else:
                cut = data.find(b"\n")
                if cut < 0:
                    if low == 0 or end - low >= MAX_BLOCK_BYTES:
                        return Block(end, end, ())
                    low = max(0, low - max(want, end - low))
                    continue
                data, start = data[cut + 1 :], first + cut + 1
            break
        return Block(start, end, _file_rows(start, data))

    def _read(self, low: int, end: int) -> bytes:
        with self._path.open("rb") as handle:
            _ = handle.seek(low)
            data = handle.read(end - low)
        self.bytes_read += len(data)
        return data

    def rows_after(self, start: int) -> Block:
        end = self.complete_end()
        if end <= start:
            return Block(start, start, ())
        return Block(start, end, _file_rows(start, self._read(start, end)))


def _file_rows(start: int, data: bytes) -> tuple[FileRow, ...]:
    return tuple(
        FileRow(raw.start, raw.end, _scrubbed(raw.row))
        for raw in rows_after(Position(start=0, offset=start, lines=0, sha256=""), data)
        if raw.row is not None
    )


def _scrubbed(row: Mapping[str, object] | None) -> Mapping[str, object]:
    if row is None:
        raise ValueError("a bad row has no content")
    _ = scrub(row)
    return row


# meiva:note
# The time a row says it was written, as an aware datetime, or None when it says none that can be read.
def stamp_of(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
