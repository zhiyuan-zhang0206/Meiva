from __future__ import annotations

import json
from dataclasses import dataclass

from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of
from base.lm.harness.session.jsonl.position import START, Position
from base.lm.harness.session.transcript.ports import PositionLostError

# meiva:note
# How far a Codex thread has been read, as the writer stores it, for one record (the main thread or a child
# thread). It has three parts. `n` is the ordinal the next stored row takes (rows the reader keeps are numbered
# one by one in the record, history rows and rollout rows alike). `history` marks the place in the thread's item
# history: a cursor the app-server gave (`thread/items/list` answers with opaque cursors; this reader never builds
# or reads one), whether that cursor is positioned AT the last item read (`inclusive`, the form a position is kept
# in: the first item the cursor yields must be that item, which is the check that the history still holds what was
# read) or after it (the form a truncated page leaves while the reader is catching up), the id of the last item
# read (`last`) and, for a record seeded part-way, `emit_first`: the first item the cursor yields is the first
# one to read, not the last one read. `rollout` is the byte span and hash of the last rollout line read (the
# same check Claude's file position makes), or None until the rollout has been read.
#
# A position that cannot be decoded is `PositionLostError`.
# meiva:link base/lm/harness/docs/transcript-projection.md


@dataclass(frozen=True, slots=True)
class HistoryMark:
    cursor: str | None = None
    inclusive: bool = False
    last: str | None = None
    emit_first: bool = False

    @property
    def fresh(self) -> bool:
        return self.cursor is None and self.last is None


@dataclass(frozen=True, slots=True)
class CodexPosition:
    n: int = 0
    history: HistoryMark = HistoryMark()
    rollout: Position = START

    @property
    def behind(self) -> bool:
        return not self.history.fresh and not self.history.inclusive


def encode(position: CodexPosition) -> str:
    mark = position.history
    rollout = position.rollout
    return json.dumps(
        {
            "v": 1,
            "n": position.n,
            "h": {
                "cursor": mark.cursor,
                "inclusive": mark.inclusive,
                "last": mark.last,
                "emit_first": mark.emit_first,
            },
            "r": {
                "start": rollout.start,
                "offset": rollout.offset,
                "lines": rollout.lines,
                "sha256": rollout.sha256,
            },
        },
        separators=(",", ":"),
    )


def decode(text: str) -> CodexPosition:
    try:
        raw = object_of(json.loads, text)
    except ValueError as error:
        raise PositionLostError(f"unreadable stored position: {text!r}") from error
    if not is_object_dict(raw) or raw.get("v") != 1:
        raise PositionLostError(f"stored position has the wrong shape: {text!r}")
    try:
        return CodexPosition(
            n=_count(raw, "n"),
            history=_mark(_part(raw, "h")),
            rollout=_rollout(_part(raw, "r")),
        )
    except _BadFieldError as error:
        raise PositionLostError(f"stored position has the wrong fields: {text!r}") from error


class _BadFieldError(Exception):
    pass


def _part(raw: dict[str, object], name: str) -> dict[str, object]:
    part = raw.get(name)
    if not is_object_dict(part):
        raise _BadFieldError(name)
    return part


def _count(raw: dict[str, object], name: str) -> int:
    value = raw.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _BadFieldError(name)
    return value


def _flag(raw: dict[str, object], name: str) -> bool:
    value = raw.get(name)
    if not isinstance(value, bool):
        raise _BadFieldError(name)
    return value


def _optional_text(raw: dict[str, object], name: str) -> str | None:
    value = raw.get(name)
    if value is not None and not isinstance(value, str):
        raise _BadFieldError(name)
    return value


def _mark(part: dict[str, object]) -> HistoryMark:
    return HistoryMark(
        cursor=_optional_text(part, "cursor"),
        inclusive=_flag(part, "inclusive"),
        last=_optional_text(part, "last"),
        emit_first=_flag(part, "emit_first"),
    )


def _rollout(part: dict[str, object]) -> Position:
    start, offset = _count(part, "start"), _count(part, "offset")
    digest = part.get("sha256")
    if start > offset or not isinstance(digest, str):
        raise _BadFieldError("rollout")
    return Position(start=start, offset=offset, lines=_count(part, "lines"), sha256=digest)
