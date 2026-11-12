from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Protocol

from base.lm.harness.session.transcript.entries import AnyEntry

# meiva:note
# How an adapter's reader hands entries to the one writer. `position` is the adapter's own opaque mark
# of how far the record has been read (Claude: the count of lines of the record's local file, Codex:
# the history cursor, Antigravity: the last step index); the writer stores it per record and hands it
# back to `read_after` after a crash, and never looks inside. A reader may push (`EntrySink.accept`,
# Claude's `session_store` callback) or be pulled (`CatchUp.read_after`, Codex's cursor, Antigravity's
# file tail); an adapter that pushes also implements `CatchUp` for the rows a crash left on disk, and
# the writer's idempotency by entry key makes the two paths safe to overlap.
# meiva:link base/lm/harness/docs/transcript-projection.md


@dataclass(frozen=True, slots=True)
class EntryBatch:
    record: str
    entries: tuple[AnyEntry, ...]
    position: str | None


class EntrySink(Protocol):
    def accept(self, batch: EntryBatch) -> Awaitable[None]: ...


class CatchUp(Protocol):
    def read_after(self, record: str, position: str | None) -> Awaitable[EntryBatch | None]: ...
