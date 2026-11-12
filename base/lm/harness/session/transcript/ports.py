from __future__ import annotations

from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from base.lm.harness.session.transcript.entries import AnyEntry

# meiva:note
# The one way an adapter's record reaches the writer: the writer asks, after the position it stored for a
# record, and the adapter answers with the complete entries past it. `position` is the adapter's own
# opaque mark of how far the record has been read (Claude: the key of the last row read and its byte
# offset in the local file; Codex: the history cursor; Antigravity: the last step index); the writer
# stores it per record in the transaction that writes the entries and never looks inside. Whatever
# wakes the writer (a stream frame, a `session_store` callback, a timer) carries no content, so the
# first read after a crash is the same code as every other read. `parse_stored` reads stored rows of one record,
# in ordinal order, with the same parser, which is how the projector's state is rebuilt after a restart and
# how a session is re-projected from the database; it carries the parser's own state across calls. `None` means nothing new. A position
# the record no longer contains is an error, never a restart from the beginning. `position_before` is the one
# way to start a record part-way: the position just before the first row that carries one of the given wire ids
# (the id of an input we sent), or the end of the record when none does, or None for a record with nothing in
# it; a session whose record already holds history reads from there, so the history is not projected again.
# `after_ordinal` is the highest ordinal already stored for the record: the rows read from the position take ordinals
# above it, so a record that was rewritten never writes a row over a stored one. `records(exhaustive=True)` is the
# listing that must not miss a record (the last read before a close): an adapter that throttles its search for the
# record's file searches at once.
# meiva:link base/lm/harness/docs/transcript-projection.md


# meiva:note
# One row of the CLI's record as it is kept in our database: the whitelisted, filtered raw row and its own
# `ordinal` in the record (sparse: rows off the whitelist are not stored). The projection reads entries
# from the CLI's rows; the stored rows are what it is re-run from after a restart or a parser fix.
# meiva:note
# A stored position the record no longer holds (the file was replaced, truncated or rewritten): raised by a reader's
# `read_after`, never a restart from the beginning. The writer puts the record at its current end and says so.
class PositionLostError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class StoredRow:
    record: str
    ordinal: int
    row: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class EntryBatch:
    record: str
    entries: tuple[AnyEntry, ...]
    position: str
    rows: tuple[StoredRow, ...] = ()
    size: int = 0


class RecordReader(Protocol):
    def read_after(self, record: str, position: str | None) -> Awaitable[EntryBatch | None]: ...

    def records(self, *, exhaustive: bool = False) -> Awaitable[tuple[str, ...]]: ...

    def parse_stored(self, rows: Sequence[StoredRow]) -> tuple[AnyEntry, ...]: ...

    def wire_id(self, key: str) -> str: ...

    def position_before(
        self, record: str, wire_ids: Sequence[str], *, after_ordinal: int = -1
    ) -> Awaitable[str | None]: ...
