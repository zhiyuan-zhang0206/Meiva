from __future__ import annotations

from collections.abc import Awaitable
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
# first read after a crash is the same code as every other read. `None` means nothing new. A position
# the record no longer contains is an error, never a restart from the beginning.
# meiva:link base/lm/harness/docs/transcript-projection.md


@dataclass(frozen=True, slots=True)
class EntryBatch:
    record: str
    entries: tuple[AnyEntry, ...]
    position: str


class RecordReader(Protocol):
    def read_after(self, record: str, position: str | None) -> Awaitable[EntryBatch | None]: ...

    def records(self) -> Awaitable[tuple[str, ...]]: ...
