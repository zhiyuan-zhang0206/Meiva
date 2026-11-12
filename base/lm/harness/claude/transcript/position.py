from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of

# meiva:note
# How far a Claude transcript file has been read, as the writer stores it: the key (`uuid`) of the last row read
# (None when that row has none), the byte span of that row (`start` to `offset`, `offset` being where the next
# read begins) and `lines`, the count of rows read, which is the next row's `Order.seq`. The offset makes the
# next read a seek; the key and the span make it checkable: the bytes of the last row are read back and must
# still be that row. A file that no longer holds it (replaced, truncated) is `PositionLost`, a fault with the
# evidence in its message and never a restart from the beginning. Only complete lines (ending in a newline) are
# ever consumed, so a row the CLI is still writing is read on a later pass.
# meiva:link base/lm/harness/docs/transcript-projection.md


class PositionLostError(Exception):
    pass


class BadRowError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Position:
    key: str | None
    start: int
    offset: int
    lines: int


@dataclass(frozen=True, slots=True)
class RawRow:
    start: int
    end: int
    row: Mapping[str, object]


START = Position(key=None, start=0, offset=0, lines=0)


def encode(position: Position) -> str:
    return json.dumps(
        {
            "key": position.key,
            "start": position.start,
            "offset": position.offset,
            "lines": position.lines,
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
    key, start, offset, lines = (
        raw.get("key"),
        raw.get("start"),
        raw.get("offset"),
        raw.get("lines"),
    )
    if (
        not (key is None or isinstance(key, str))
        or not isinstance(start, int)
        or not isinstance(offset, int)
        or not isinstance(lines, int)
    ):
        raise PositionLostError(f"stored position has the wrong fields: {text!r}")
    return Position(key=key, start=start, offset=offset, lines=lines)


def verify(position: Position, last_row: bytes) -> None:
    if position.lines == 0:
        return
    if not last_row.endswith(b"\n"):
        raise PositionLostError(
            f"the file no longer ends the last row read at byte {position.offset}"
        )
    row = _object(last_row.decode("utf-8", errors="replace"), position.start)
    found = row.get("uuid")
    if position.key is not None and found != position.key:
        raise PositionLostError(
            f"the row at byte {position.start} is {found!r}, expected {position.key!r}"
        )


def rows_after(position: Position, data: bytes) -> tuple[RawRow, ...]:
    rows: list[RawRow] = []
    cursor = 0
    while (newline := data.find(b"\n", cursor)) >= 0:
        text = data[cursor:newline].decode("utf-8")
        if text.strip():
            rows.append(
                RawRow(
                    start=position.offset + cursor,
                    end=position.offset + newline + 1,
                    row=_object(text, position.offset + cursor),
                )
            )
        cursor = newline + 1
    return tuple(rows)


def advance(position: Position, rows: Sequence[RawRow]) -> Position:
    if not rows:
        return position
    last = rows[-1]
    key = last.row.get("uuid")
    return Position(
        key=key if isinstance(key, str) else None,
        start=last.start,
        offset=last.end,
        lines=position.lines + len(rows),
    )


def _object(text: str, offset: int) -> Mapping[str, object]:
    try:
        raw = object_of(json.loads, text)
    except ValueError as error:
        raise BadRowError(f"a complete row at byte {offset} is not JSON") from error
    if not is_object_dict(raw):
        raise BadRowError(f"a complete row at byte {offset} is not an object")
    return raw
