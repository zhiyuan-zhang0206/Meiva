from __future__ import annotations

import threading
from collections.abc import Buffer, Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from io import BufferedReader, RawIOBase
from pathlib import Path
from typing import override

import zstandard

from base.lm.harness.session.transcript.entries import Anomaly
from base.lm.harness.session.transcript.position import Position, rows_after
from base.lm.harness.session.transcript.projection import REPORTED_LIMIT, RecentSet
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


@dataclass(frozen=True, slots=True)
class QuoteRow:
    row: FileRow
    is_tail: Callable[[], bool]


class RecordReadBudgetError(ValueError):
    pass


class RecordFile:
    def __init__(self, path: Path) -> None:
        self._path = path
        self.bytes_read = 0
        self._floor = 0
        self._read_budget: int | None = None

    def prepare_reading(self, offset: int) -> None:
        self._read_budget = self.bytes_read + MAX_BLOCK_BYTES
        self.prepare_anchor(offset)

    @contextmanager
    def quote_row(self, offset: int, *, inbound: bool) -> Generator[QuoteRow | None]:
        del inbound
        self.prepare_reading(offset)
        row = self.row_at(offset)
        yield QuoteRow(row, lambda: row.end == self.complete_record_end()) if row else None

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
        if self._read_budget is not None:
            raise RecordReadBudgetError("The target row exceeds the record reading budget")
        return None

    def revision(self) -> int:
        return 0

    def watch_signature(self) -> tuple[int, ...] | None:
        try:
            stat = self._path.stat()
        except OSError:
            return None
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)

    def size(self) -> int:
        return self._path.stat().st_size

    # meiva:note
    # The full record's last committed row boundary, even when an anchored reader retains only an older window.
    def complete_record_end(self) -> int:
        return self.complete_end()

    # meiva:note
    # The end of the last complete line: a row the CLI is still writing is not read.
    def complete_end(self) -> int:
        position = self.size()
        while position > 0:
            low = max(0, position - _CHUNK_BYTES)
            data = self._read(low, position)
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
        if self._read_budget is not None and self.bytes_read + end - low > self._read_budget:
            raise RecordReadBudgetError("The record reading window exceeds its byte budget")
        with self._path.open("rb") as handle:
            _ = handle.seek(low)
            data = handle.read(end - low)
        self.bytes_read += len(data)
        return data

    # meiva:note
    # A bounded forward block starts at a verified row end. A clipped final
    # line stays outside the projection and is not an end-of-source claim.
    # meiva:link base/lm/harness/docs/transcript-projection.md
    def rows_forward(self, start: int, want: int = BLOCK_BYTES) -> Block:
        end = min(self.size(), start + want)
        if end <= start:
            return Block(start, start, ())
        data = self._read(start, end)
        cut = data.rfind(b"\n")
        if cut < 0:
            return Block(start, start, ())
        data = data[: cut + 1]
        return Block(start, start + len(data), _file_rows(start, data))

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
# The rows of a record that its parser could not use, said once for each kind (the CLI, the anomaly and its detail)
# by the process that reads records. The record is the CLI's own and moves with its version, so a row of a kind this
# code does not know is skipped, never guessed at; the report is how a kind a newer CLI began to write is noticed
# instead of being silently absent from the timeline. A parser lives for one read and a page is read again on every
# poll, so the memory of what was said is not the parser's: one instance belongs to the process (the ops daemon for
# every timeline it answers, a live sink for its turn) and is given to each parser it builds. What it remembers is
# bounded like the projector's own reports (`REPORTED_LIMIT`, the latest kinds), and a restart says each kind again.
# The ops daemon reads from several worker threads, hence the lock.
# meiva:link base/lm/harness/docs/transcript-projection.md
class UnreadRows:
    def __init__(self) -> None:
        self._reported: RecentSet[tuple[str, str, str]] = RecentSet(REPORTED_LIMIT)
        self._lock = threading.Lock()

    def report(self, cli: str, anomaly: Anomaly) -> None:
        mark = (cli, anomaly.anomaly.value, anomaly.detail)
        with self._lock:
            if mark in self._reported:
                return
            self._reported.add(mark)
        logger.warning(
            "a row of the {cli} record is not read: {kind} {detail}",
            event="harness_record_row_unread",
            cli=cli,
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
        self.compressed_bytes_read = 0
        self.stream_passes = 0

    def prepare(self, before: int | None) -> None:
        self._prepare(before, inclusive=False)

    def prepare_anchor(self, offset: int) -> None:
        self._prepare(offset, inclusive=True)

    # meiva:note
    # Retain the complete target before allocating its remaining 16MiB region
    # to older/newer rows. The archive still takes one complete streaming pass.
    # meiva:link base/lm/harness/docs/transcript-projection.md
    def prepare_reading(self, offset: int) -> None:
        retained = _ReadingBytes(offset)
        position, complete = 0, 0
        with self._path.open("rb") as handle:
            self.stream_passes += 1
            reader = zstandard.ZstdDecompressor().stream_reader(handle, read_across_frames=True)
            while chunk := reader.read(_STREAM_CHUNK_BYTES):
                self.bytes_read += len(chunk)
                position += len(chunk)
                newline = chunk.rfind(b"\n")
                if newline >= 0:
                    complete = position - len(chunk) + newline + 1
                retained.feed(chunk, position)
            self.compressed_bytes_read += handle.tell()
        self._install_tail(retained.data, retained.start + len(retained.data), position, complete)

    @contextmanager
    def quote_row(self, offset: int, *, inbound: bool) -> Generator[QuoteRow | None]:
        del inbound
        if offset >= MAX_BLOCK_BYTES:
            raise RecordReadBudgetError("The quote offset exceeds the archive read budget")
        with self._path.open("rb") as handle:
            compressed = _QuoteStream(handle.read)
            with zstandard.ZstdDecompressor().stream_reader(
                BufferedReader(compressed), read_across_frames=True
            ) as reader:
                decoded = _QuoteStream(reader.read)
                self.stream_passes += 1
                try:
                    yield _quote_from_stream(decoded.read, offset)
                finally:
                    self.bytes_read += decoded.bytes_read
                    self.compressed_bytes_read += compressed.bytes_read

    def _prepare(self, before: int | None, *, inclusive: bool) -> None:
        ring = bytearray()
        position, kept_end = 0, None
        complete_record_end = 0
        with self._path.open("rb") as handle:
            self.stream_passes += 1
            reader = zstandard.ZstdDecompressor().stream_reader(handle, read_across_frames=True)
            while chunk := reader.read(_STREAM_CHUNK_BYTES):
                self.bytes_read += len(chunk)
                position += len(chunk)
                newline = chunk.rfind(b"\n")
                if newline >= 0:
                    complete_record_end = position - len(chunk) + newline + 1
                if kept_end is not None:
                    continue
                kept_end = _retain_anchor_chunk(ring, chunk, position, before, inclusive=inclusive)
            self.compressed_bytes_read += handle.tell()
        end = position if kept_end is None else kept_end
        self._install_tail(ring, end, position, complete_record_end)

    def _install_tail(self, ring: bytearray, end: int, total: int, complete: int) -> None:
        start = end - len(ring)
        first = bytes(ring).find(b"\n") if start > 0 else -1
        if start > 0:
            self._tail = bytes(ring[first + 1 :]) if first >= 0 else b""
            self._floor = end if first < 0 else start + first + 1
        else:
            self._tail = bytes(ring)
            self._floor = 0
        self._total = total
        self._complete_record_end = complete

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

    def rows_forward(self, start: int, want: int = BLOCK_BYTES) -> Block:
        return super().rows_forward(start, min(want, max(0, self.complete_end() - start)))

    def rows_after(self, start: int) -> Block:
        raise NotImplementedError("a compressed record is read as a window, never followed")


# meiva:link base/lm/harness/docs/transcript-projection.md
class _QuoteStream(RawIOBase):
    def __init__(self, read: Callable[[int], bytes]) -> None:
        super().__init__()
        self._read = read
        self.bytes_read = 0

    @override
    def readable(self) -> bool:
        return True

    @override
    def readinto(self, buffer: Buffer, /) -> int:
        view = memoryview(buffer).cast("B")
        data = self.read(len(view))
        view[: len(data)] = data
        return len(data)

    @override
    def read(self, size: int = -1, /) -> bytes:
        remaining = MAX_BLOCK_BYTES - self.bytes_read
        if remaining <= 0:
            raise RecordReadBudgetError("The archive quote exceeds its byte budget")
        requested = size if size >= 0 else _CHUNK_BYTES
        data = self._read(min(remaining, requested, _CHUNK_BYTES))
        self.bytes_read += len(data)
        return data


def _quote_from_stream(read: Callable[[int], bytes], offset: int) -> QuoteRow | None:
    if offset < 0:
        return None
    remaining, previous = offset, b""
    while remaining:
        chunk = read(remaining)
        if not chunk:
            return None
        previous = chunk[-1:]
        remaining -= len(chunk)
    if offset and previous != b"\n":
        return None
    line = bytearray()
    while data := read(_CHUNK_BYTES):
        newline = data.find(b"\n")
        if newline >= 0:
            line.extend(data[: newline + 1])
            rows = _file_rows(offset, bytes(line))
            return (
                QuoteRow(rows[0], partial(_quote_is_tail, read, data[newline + 1 :]))
                if rows
                else None
            )
        line.extend(data)
    return None


def _quote_is_tail(read: Callable[[int], bytes], data: bytes) -> bool:
    if b"\n" in data:
        return False
    while chunk := read(_CHUNK_BYTES):
        if b"\n" in chunk:
            return False
    return True


def _retain_anchor_chunk(
    ring: bytearray, chunk: bytes, position: int, before: int | None, *, inclusive: bool
) -> int | None:
    kept_end = None
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
    return kept_end


@dataclass
class _ReadingBytes:
    offset: int
    data: bytearray = field(default_factory=bytearray)
    start: int = 0
    stop: int | None = None

    def feed(self, chunk: bytes, position: int) -> None:
        if self.stop is not None:
            low = max(position - len(chunk), self.start + len(self.data))
            high = min(position, self.stop)
            if high > low:
                self.data += chunk[low - (position - len(chunk)) : high - (position - len(chunk))]
            return
        self.data += chunk
        origin = position - len(self.data)
        newline = (
            self.data.find(b"\n", max(0, self.offset - origin)) if position > self.offset else -1
        )
        if newline >= 0:
            self._anchor_complete(origin, origin + newline + 1)
            return
        if position - self.offset > MAX_BLOCK_BYTES:
            raise RecordReadBudgetError("The target row exceeds the retained byte budget")
        if len(self.data) > MAX_BLOCK_BYTES:
            del self.data[: len(self.data) - MAX_BLOCK_BYTES]
        self.start = position - len(self.data)

    def _anchor_complete(self, origin: int, end: int) -> None:
        slack = MAX_BLOCK_BYTES - (end - self.offset)
        if slack < 0 or self.offset < origin:
            raise RecordReadBudgetError("The target row exceeds the retained byte budget")
        self.start = max(origin, self.offset - slack // 2)
        self.stop = end + slack - (self.offset - self.start)
        del self.data[self.stop - origin :]
        del self.data[: self.start - origin]
