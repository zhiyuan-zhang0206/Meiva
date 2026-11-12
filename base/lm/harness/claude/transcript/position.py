from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of

# meiva:note
# How far a Claude transcript file has been read, as the writer stores it: the byte span of the last row read
# (`start` to `offset`, `offset` being where the next read begins), the SHA-256 of that row's bytes (`sha256`)
# and `lines`, the count of rows read, which is the next row's `Order.seq`. The offset makes the next read a
# seek; the span and the hash make it checkable without parsing anything: the bytes of the last row are read
# back and hashed, and must still be that row, whether or not the row has a `uuid` and however large it is. A
# file that no longer holds it (replaced, truncated, rewritten) is `PositionLostError`, a fault with the
# evidence in its message and never a restart from the beginning. Only complete lines (ending in a newline)
# are ever consumed, so a row the CLI is still writing is read on a later pass.
#
# A complete line that cannot be used is never a fault and never stops the read: it is returned as a `RawRow`
# with `bad` naming why (`BAD_*`: invalid UTF-8, not JSON, not an object, nested too deep to decode, larger
# than `MAX_ROW_BYTES`), it counts as a line so the position moves past it, and the reader reports it as an
# anomaly. One bad row must not keep every later row from being read.
# meiva:link base/lm/harness/docs/transcript-projection.md

BAD_UTF8 = "invalid_utf8"
BAD_JSON = "not_json"
BAD_OBJECT = "not_object"
BAD_DEPTH = "too_deep"
BAD_SIZE = "oversize"


class PositionLostError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Position:
    start: int
    offset: int
    lines: int
    sha256: str


@dataclass(frozen=True, slots=True)
class RawRow:
    start: int
    end: int
    row: Mapping[str, object] | None
    bad: str | None = None
    sha256: str | None = None


START = Position(start=0, offset=0, lines=0, sha256="")


def encode(position: Position) -> str:
    return json.dumps(
        {
            "start": position.start,
            "offset": position.offset,
            "lines": position.lines,
            "sha256": position.sha256,
        },
        separators=(",", ":"),
    )


def decode(text: str) -> Position:
    try:
        raw = object_of(json.loads, text)
    except ValueError as error:
        raise PositionLostError(f"unreadable stored position: {text!r}") from error
    if not is_object_dict(raw):
        raise PositionLostError(f"stored position is not an object: {text!r}")
    start, offset, lines, digest = (
        raw.get("start"),
        raw.get("offset"),
        raw.get("lines"),
        raw.get("sha256"),
    )
    if (
        not isinstance(start, int)
        or not isinstance(offset, int)
        or not isinstance(lines, int)
        or not isinstance(digest, str)
        or not 0 <= start <= offset
    ):
        raise PositionLostError(f"stored position has the wrong fields: {text!r}")
    return Position(start=start, offset=offset, lines=lines, sha256=digest)


def verify(position: Position, *, length: int, sha256: str) -> None:
    if position.lines == 0:
        return
    if length != position.offset - position.start:
        raise PositionLostError(
            f"the file ends at {position.start + length}, before the last row read "
            f"({position.start}..{position.offset})"
        )
    if sha256 != position.sha256:
        raise PositionLostError(
            f"the bytes at {position.start}..{position.offset} are no longer the last row read"
        )


def row_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def rows_after(position: Position, data: bytes) -> tuple[RawRow, ...]:
    rows: list[RawRow] = []
    cursor = 0
    while (newline := data.find(b"\n", cursor)) >= 0:
        line = data[cursor:newline]
        if line.strip():
            start, end = position.offset + cursor, position.offset + newline + 1
            rows.append(_row(line, start, end))
        cursor = newline + 1
    return tuple(rows)


def advance(position: Position, rows: Sequence[RawRow], data: bytes) -> Position:
    if not rows:
        return position
    last = rows[-1]
    digest = last.sha256
    if digest is None:
        digest = row_digest(data[last.start - position.offset : last.end - position.offset])
    return Position(
        start=last.start,
        offset=last.end,
        lines=position.lines + len(rows),
        sha256=digest,
    )


def _row(line: bytes, start: int, end: int) -> RawRow:
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError:
        return RawRow(start, end, None, BAD_UTF8)
    try:
        raw = object_of(json.loads, text)
    except RecursionError:
        return RawRow(start, end, None, BAD_DEPTH)
    except ValueError:
        return RawRow(start, end, None, BAD_JSON)
    if not is_object_dict(raw):
        return RawRow(start, end, None, BAD_OBJECT)
    return RawRow(start, end, raw)
