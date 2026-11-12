from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from base.lm.harness.claude.reconciliation.transcript import transcript_path
from base.lm.harness.claude.transcript.parse import ParseState, parse_row
from base.lm.harness.claude.transcript.position import (
    BAD_DEPTH,
    BAD_SIZE,
    START,
    Position,
    PositionLostError,
    RawRow,
    advance,
    decode,
    encode,
    rows_after,
    verify,
)
from base.lm.harness.claude.transcript.store_filter import scrub, stored_row
from base.lm.harness.session.transcript.entries import (
    MAIN_RECORD,
    Anomaly,
    AnomalyKind,
    AnyEntry,
    Order,
)
from base.lm.harness.session.transcript.ports import EntryBatch, StoredRow

# meiva:note
# The one reader of a Claude session's transcript: it implements `RecordReader` by reading the CLI's local
# JSONL after the stored position, whatever woke the writer (a stream frame, the `session_store` callback, a
# poll) and whether the process just started or has been running for hours. The main record is the session's
# file; a sub-agent's record is `<session>/subagents/agent-<id>.jsonl` next to it.
#
# One call returns at most `MAX_BATCH_BYTES` of complete lines (about, a row is never split), so catching up
# on a large file is a series of calls, each a bounded batch and transaction, that the writer repeats until
# the answer is `None`. A single row larger than a batch is read whole up to `MAX_ROW_BYTES`; a longer one is
# skipped unread (its end is found by streaming) and reported as a bad row. Each row is scrubbed (`scrub`)
# before it is parsed or filtered, so the timeline and the stored rows agree and both are storable.
#
# The only memory it keeps is each record's parse state (the start of the open tool calls, for durations),
# keyed by the position it belongs to, never advanced past what the caller holds: a read from the position it
# was given is a function of that position, so a batch whose database write failed is read again with the
# state it had the first time. A restart loses the state: a result whose call was read before the restart
# then has no duration.
# meiva:link base/lm/harness/docs/transcript-projection.md

MAX_BATCH_BYTES = 4 * 1024 * 1024
MAX_ROW_BYTES = 64 * 1024 * 1024
_STREAM_CHUNK_BYTES = 1024 * 1024
_REMEMBERED_POSITIONS = 2


@dataclass(frozen=True, slots=True)
class _Chunk:
    data: bytes
    skipped: RawRow | None = None


class ClaudeRecordReader:
    def __init__(
        self,
        *,
        claude_home: Path,
        session_id: str,
        max_batch_bytes: int = MAX_BATCH_BYTES,
        max_row_bytes: int = MAX_ROW_BYTES,
    ) -> None:
        self._home = claude_home
        self._session_id = session_id
        self._max_batch = max_batch_bytes
        self._max_row = max_row_bytes
        self._states: dict[str, dict[str, ParseState]] = {}
        self._stored_states: dict[str, ParseState] = {}
        self.bytes_read = 0

    def parse_stored(self, rows: Sequence[StoredRow]) -> tuple[AnyEntry, ...]:
        entries: list[AnyEntry] = []
        for stored in rows:
            state = self._stored_states.get(stored.record, ParseState())
            state, parsed = parse_row(state, stored.row, line=stored.ordinal)
            self._stored_states[stored.record] = state
            entries.extend(parsed)
        return tuple(entries)

    async def records(self) -> tuple[str, ...]:
        return await asyncio.to_thread(self._records)

    async def read_after(self, record: str, position: str | None) -> EntryBatch | None:
        return await asyncio.to_thread(self._read, record, position)

    def _main(self) -> Path | None:
        return transcript_path(self._home, self._session_id)

    def _path(self, record: str) -> Path | None:
        main = self._main()
        if record == MAIN_RECORD or main is None:
            return main
        return main.parent / self._session_id / "subagents" / f"agent-{record}.jsonl"

    def _records(self) -> tuple[str, ...]:
        main = self._main()
        if main is None:
            return ()
        folder = main.parent / self._session_id / "subagents"
        children = sorted(
            path.name.removeprefix("agent-").removesuffix(".jsonl")
            for path in (folder.glob("agent-*.jsonl") if folder.is_dir() else ())
        )
        return (MAIN_RECORD, *children)

    def _read(self, record: str, stored: str | None) -> EntryBatch | None:
        position = START if stored is None else decode(stored)
        path = self._path(record)
        if path is None or not path.is_file():
            if position.lines:
                raise PositionLostError(f"the transcript of record {record!r} is gone")
            return None
        with path.open("rb") as handle:
            self._verify(handle, position)
            chunk = self._next_chunk(handle, position)
        rows = (*rows_after(position, chunk.data), *((chunk.skipped,) if chunk.skipped else ()))
        if not rows:
            return None
        cache = self._states.setdefault(record, {})
        state = cache.get(stored or "", ParseState())
        entries: list[AnyEntry] = []
        kept: list[StoredRow] = []
        for index, row in enumerate(rows):
            ordinal = position.lines + index
            state, parsed, filtered = self._row(record, ordinal, row, state)
            entries.extend(parsed)
            if filtered is not None:
                kept.append(StoredRow(record, ordinal, filtered))
        moved = encode(advance(position, rows, chunk.data))
        cache[moved] = state
        while len(cache) > _REMEMBERED_POSITIONS:
            del cache[next(iter(cache))]
        return EntryBatch(record, tuple(entries), moved, tuple(kept))

    def _row(
        self, record: str, ordinal: int, row: RawRow, state: ParseState
    ) -> tuple[ParseState, tuple[AnyEntry, ...], dict[str, object] | None]:
        if row.row is None:
            return state, (_bad_row(record, ordinal, row.bad),), None
        try:
            _ = scrub(row.row)
            moved, parsed = parse_row(state, row.row, line=ordinal)
            filtered = stored_row(row.row, subagent=record != MAIN_RECORD)
        except RecursionError:
            return state, (_bad_row(record, ordinal, BAD_DEPTH),), None
        return moved, parsed, filtered

    def _verify(self, handle: BinaryIO, position: Position) -> None:
        if not position.lines:
            return
        _ = handle.seek(position.start)
        digest = hashlib.sha256()
        length, wanted = 0, position.offset - position.start
        while length < wanted:
            piece = handle.read(min(_STREAM_CHUNK_BYTES, wanted - length))
            if not piece:
                break
            digest.update(piece)
            length += len(piece)
        self.bytes_read += length
        verify(position, length=length, sha256=digest.hexdigest())

    def _next_chunk(self, handle: BinaryIO, position: Position) -> _Chunk:
        _ = handle.seek(position.offset)
        data = handle.read(self._max_batch)
        self.bytes_read += len(data)
        cut = data.rfind(b"\n")
        if cut >= 0:
            return _Chunk(data[: cut + 1])
        if len(data) < self._max_batch:
            return _Chunk(b"")
        return self._one_long_row(handle, position, data)

    def _one_long_row(self, handle: BinaryIO, position: Position, head: bytes) -> _Chunk:
        digest = hashlib.sha256(head)
        length = len(head)
        kept = [head]
        while True:
            piece = handle.read(_STREAM_CHUNK_BYTES)
            self.bytes_read += len(piece)
            if not piece:
                return _Chunk(b"")
            newline = piece.find(b"\n")
            used = piece if newline < 0 else piece[: newline + 1]
            digest.update(used)
            length += len(used)
            if length <= self._max_row:
                kept.append(used)
            if newline >= 0:
                break
        if length <= self._max_row:
            return _Chunk(b"".join(kept))
        start = position.offset
        return _Chunk(b"", RawRow(start, start + length, None, BAD_SIZE, digest.hexdigest()))


def _bad_row(record: str, ordinal: int, reason: str | None) -> Anomaly:
    detail = reason or "unusable"
    return Anomaly(
        key=f"claude:anomaly:bad-row:{record}:{ordinal}",
        order=Order(ordinal),
        record=record,
        anomaly=AnomalyKind.BAD_ROW,
        detail=detail,
    )
