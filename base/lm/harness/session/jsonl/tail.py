from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import BinaryIO

from base.lm.harness.session.jsonl.position import (
    BAD_SIZE,
    Position,
    RawRow,
    row_digest,
    verify,
)

# meiva:note
# Reading the bytes of a JSONL record file after a stored position, for every reader of one: check that the last row
# read is still where it was (`verify_last_row` reads those bytes back and hashes them; nothing is decoded), then take
# the complete lines after it, at most `max_batch` bytes of them (`next_chunk`; a row is never split, and a trailing
# line without a newline is left for a later pass). A single row longer than a batch is read whole up to `max_row`;
# a longer one is skipped unread (its end is found by streaming) and returned as a bad row of size. `end_position`
# is the position just after the file's last complete row, found by reading the end of the file backwards, never
# the whole file. Every function returns the number of bytes it read so a caller can account for them.
# meiva:link base/lm/harness/docs/transcript-projection.md

STREAM_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class Chunk:
    data: bytes
    skipped: RawRow | None
    bytes_read: int


def verify_last_row(handle: BinaryIO, position: Position) -> int:
    if not position.lines:
        return 0
    _ = handle.seek(position.start)
    digest = hashlib.sha256()
    length, wanted = 0, position.offset - position.start
    while length < wanted:
        piece = handle.read(min(STREAM_CHUNK_BYTES, wanted - length))
        if not piece:
            break
        digest.update(piece)
        length += len(piece)
    verify(position, length=length, sha256=digest.hexdigest())
    return length


def next_chunk(handle: BinaryIO, position: Position, *, max_batch: int, max_row: int) -> Chunk:
    _ = handle.seek(position.offset)
    data = handle.read(max_batch)
    cut = data.rfind(b"\n")
    if cut >= 0:
        return Chunk(data[: cut + 1], None, len(data))
    if len(data) < max_batch:
        return Chunk(b"", None, len(data))
    return _one_long_row(handle, position, data, max_row)


def _one_long_row(handle: BinaryIO, position: Position, head: bytes, max_row: int) -> Chunk:
    digest = hashlib.sha256(head)
    length = len(head)
    read = len(head)
    kept = [head]
    while True:
        piece = handle.read(STREAM_CHUNK_BYTES)
        read += len(piece)
        if not piece:
            return Chunk(b"", None, read)
        newline = piece.find(b"\n")
        used = piece if newline < 0 else piece[: newline + 1]
        digest.update(used)
        length += len(used)
        if length <= max_row:
            kept.append(used)
        if newline >= 0:
            break
    if length <= max_row:
        return Chunk(b"".join(kept), None, read)
    start = position.offset
    return Chunk(b"", RawRow(start, start + length, None, BAD_SIZE, digest.hexdigest()), read)


# meiva:note
# The position just after the last non-blank complete line of the file, or None when it has none. The file is read
# backwards in `STREAM_CHUNK_BYTES` pieces until the start of that line is found (a row longer than `max_row` is
# not followed further back: the position then has no hash to verify and is None). `lines` is 1, only to say that
# a row has been read; the count of rows is the caller's own.
def end_position(handle: BinaryIO, *, max_row: int) -> tuple[Position | None, int]:
    size = handle.seek(0, 2)
    end = size
    read = 0
    while end > 0:
        window = min(STREAM_CHUNK_BYTES, end)
        _ = handle.seek(end - window)
        block = handle.read(window)
        read += len(block)
        newline = block.rfind(b"\n")
        if newline >= 0:
            end = end - window + newline + 1
            break
        end -= window
    else:
        return None, read
    line_end = end
    while line_end > 0:
        found, used = _last_line(handle, line_end, max_row)
        read += used
        if found is None:
            return None, read
        start, data = found
        if data.strip():
            return Position(
                start=start, offset=line_end, lines=1, sha256=row_digest(data + b"\n")
            ), read
        line_end = start
    return None, read


def _last_line(handle: BinaryIO, end: int, max_row: int) -> tuple[tuple[int, bytes] | None, int]:
    pieces: list[bytes] = []
    cursor = end - 1
    read = 0
    total = 0
    while cursor > 0:
        window = min(STREAM_CHUNK_BYTES, cursor)
        _ = handle.seek(cursor - window)
        block = handle.read(window)
        read += len(block)
        newline = block.rfind(b"\n")
        if newline >= 0:
            pieces.append(block[newline + 1 :])
            start = cursor - window + newline + 1
            return (start, b"".join(reversed(pieces))), read
        pieces.append(block)
        total += len(block)
        cursor -= window
        if total > max_row:
            return None, read
    return (0, b"".join(reversed(pieces))), read
