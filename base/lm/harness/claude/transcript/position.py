from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of

# meiva:note
# The rows of a span of a Claude transcript file, found without parsing anything: `rows_after` splits the bytes read
# from a line boundary (`Position.offset` is where they begin in the file) into complete lines (ending in a newline;
# a row the CLI is still writing is read on a later pass) and returns each with its byte span.
#
# A complete line that cannot be used is never a fault and never stops the read: it is returned as a `RawRow`
# with `bad` naming why (`BAD_*`: invalid UTF-8, not JSON, not an object, nested too deep to decode), it counts as a
# row so the position moves past it, and the reader reports it as an anomaly. One bad row must not keep every later
# row from being read.
# meiva:link base/lm/harness/docs/transcript-projection.md

BAD_UTF8 = "invalid_utf8"
BAD_JSON = "not_json"
BAD_OBJECT = "not_object"
BAD_DEPTH = "too_deep"


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
