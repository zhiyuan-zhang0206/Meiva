from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import zstandard

from base.lm.harness.session.transcript.entries import Anomaly
from base.lm.harness.session.transcript.position import Position, rows_after
from base.lm.harness.session.transcript.scrub import scrub
from base.log import logger

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
_STREAM_CHUNK_BYTES = 1024 * 1024


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
        self._floor = 0

    # meiva:note
    # Gets the file ready for a window that ends before the row at `before` (None: at the end). A plain file needs nothing.
    def prepare(self, before: int | None) -> None:
        del before

    def prepare_anchor(self, offset: int) -> None:
        del offset
        self.prepare(None)

    # meiva:note
    # Item IDs name row starts. An inclusive lookup must find the complete
    # newline-terminated row here, where byte offsets have their meaning.
    # meiva:link base/lm/harness/docs/transcript-projection.md
    def row_at(self, offset: int) -> FileRow | None:
        if offset < self._floor or offset < 0 or offset >= self.size():
            return None
        if offset > 0 and self._read(offset - 1, offset) != b"\n":
            return None
        chunks: list[bytes] = []
        at, remaining = offset, MAX_BLOCK_BYTES
        while remaining > 0:
            data = self._read(at, min(self.size(), at + min(_CHUNK_BYTES, remaining)))
            if not data:
                return None
            newline = data.find(b"\n")
            if newline >= 0:
                chunks.append(data[: newline + 1])
                rows = _file_rows(offset, b"".join(chunks))
                return rows[0] if len(rows) == 1 and rows[0].offset == offset else None
            chunks.append(data)
            at += len(data)
            remaining -= len(data)
        return None

    def revision(self) -> int:
        return 0

    def size(self) -> int:
        return self._path.stat().st_size

    # meiva:note
    # The full record's last committed row boundary, even when an anchored reader retains only an older window.
    def complete_record_end(self) -> int:
        return self.complete_end()

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
    # first line boundary inside it, widened while one row is longer than the block (up to `MAX_BLOCK_BYTES`, past which the
    # block is empty and the window ends before that row). `start == 0` means the file's first row is in it.
    def rows_before(self, end: int, want: int = BLOCK_BYTES) -> Block:
        low = max(self._floor, end - want)
        while True:
            data = self._read(max(0, low - 1), end)
            first = max(0, low - 1)
            if low == 0:
                start = 0
            elif data[:1] == b"\n":
                data, start = data[1:], low
            else:
                cut = data.find(b"\n")
                if cut < 0 or first + cut + 1 >= end:
                    if low <= self._floor or end - low >= MAX_BLOCK_BYTES:
                        return Block(end, end, ())
                    low = max(self._floor, low - max(want, end - low))
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
# The rows of a record that its parser could not use, said once for each kind by one reader. The record is the CLI's
# own and moves with its version, so a row of a kind this code does not know is skipped, never guessed at; the report
# is how a kind a newer CLI began to write is noticed instead of being silently absent from the timeline.
class UnreadRows:
    def __init__(self, cli: str) -> None:
        self._cli = cli
        self._reported: set[tuple[str, str]] = set()

    def report(self, anomaly: Anomaly) -> None:
        mark = (anomaly.anomaly.value, anomaly.detail)
        if mark in self._reported:
            return
        self._reported.add(mark)
        logger.warning(
            "a row of the {cli} record is not read: {kind} {detail}",
            event="harness_record_row_unread",
            cli=self._cli,
            kind=anomaly.anomaly.value,
            detail=anomaly.detail,
        )


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


# meiva:note
# A rollout the CLI has compressed (`.jsonl.zst`, one zstd stream of exactly the bytes of the plain file; the CLI restores
# the plain file under its own name when it resumes the thread). A row's offset is its offset in the decompressed bytes, so
# a row has the same name here as in the plain file. `prepare` makes one streaming pass: it counts the decompressed bytes and
# keeps in memory only the last `MAX_BLOCK_BYTES` before the window's end (before the row at `before`, or the end of the
# stream), and a window is then read from that tail like from a file whose first `floor` bytes are not there: the window
# ends before that point, with `has_more`. Nothing is read twice and nothing larger than the tail is held. A file that
# cannot be decoded raises `zstandard.ZstdError`.
class ZstdRecordFile(RecordFile):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self._tail = b""
        self._total = 0
        self._complete_record_end = 0

    def prepare(self, before: int | None) -> None:
        self._prepare(before, inclusive=False)

    def prepare_anchor(self, offset: int) -> None:
        self._prepare(offset, inclusive=True)

    def _prepare(self, before: int | None, *, inclusive: bool) -> None:
        ring = bytearray()
        position, kept_end = 0, None
        complete_record_end = 0
        with self._path.open("rb") as handle:
            reader = zstandard.ZstdDecompressor().stream_reader(handle, read_across_frames=True)
            while chunk := reader.read(_STREAM_CHUNK_BYTES):
                self.bytes_read += len(chunk)
                position += len(chunk)
                newline = chunk.rfind(b"\n")
                if newline >= 0:
                    complete_record_end = position - len(chunk) + newline + 1
                if kept_end is not None:
                    continue
                ring += chunk
                if before is not None and position >= before:
                    boundary = before
                    if inclusive:
                        origin = position - len(ring)
                        newline = ring.find(b"\n", max(0, before - origin))
                        boundary = origin + newline + 1 if newline >= 0 else position + 1
                    if boundary <= position:
                        del ring[len(ring) - (position - boundary) :]
                        kept_end = boundary
                if len(ring) > MAX_BLOCK_BYTES:
                    del ring[: len(ring) - MAX_BLOCK_BYTES]
        end = position if kept_end is None else kept_end
        start = end - len(ring)
        first = bytes(ring).find(b"\n") if start > 0 else -1
        if start > 0:
            self._tail = bytes(ring[first + 1 :]) if first >= 0 else b""
            self._floor = end if first < 0 else start + first + 1
        else:
            self._tail = bytes(ring)
            self._floor = 0
        self._total = position
        self._complete_record_end = complete_record_end

    @property
    def held_bytes(self) -> int:
        return len(self._tail)

    def size(self) -> int:
        return self._total

    def complete_record_end(self) -> int:
        return self._complete_record_end

    def complete_end(self) -> int:
        cut = self._tail.rfind(b"\n")
        return self._floor + cut + 1 if cut >= 0 else self._floor

    def _read(self, low: int, end: int) -> bytes:
        known = (b"\n" if self._floor > 0 else b"") + self._tail
        origin = max(self._floor - 1, 0)
        return known[low - origin : end - origin]

    def rows_after(self, start: int) -> Block:
        raise NotImplementedError("a compressed record is read as a window, never followed")
