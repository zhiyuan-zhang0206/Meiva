from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from base.lm.harness.claude.delivery import key_uuid
from base.lm.harness.claude.reconciliation.transcript import claude_home as resolve_claude_home
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
    row_digest,
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
        session_id: str | Callable[[], str | None],
        workspace: Path | None = None,
        max_batch_bytes: int = MAX_BATCH_BYTES,
        max_row_bytes: int = MAX_ROW_BYTES,
    ) -> None:
        self._home = claude_home
        self._session_source = (lambda: session_id) if isinstance(session_id, str) else session_id
        self._workspace = workspace
        self._pinned: tuple[str, Path] | None = None
        self._max_batch = max_batch_bytes
        self._max_row = max_row_bytes
        self._states: dict[str, dict[str, ParseState]] = {}
        self._stored_states: dict[str, ParseState] = {}
        self.bytes_read = 0

    @classmethod
    def for_session(
        cls,
        *,
        environ: Mapping[str, str],
        workspace: Path,
        session_id: Callable[[], str | None],
    ) -> ClaudeRecordReader:
        return cls(
            claude_home=resolve_claude_home(None, environ),
            session_id=session_id,
            workspace=workspace,
        )

    def wire_id(self, key: str) -> str:
        return key_uuid(key)

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

    # meiva:note
    # The session's own file, `projects/<project>/<session id>.jsonl`. The session id is unique, but the CLI can
    # leave the same one under several project directories (a resume from another working directory copies the
    # file). The directory of the agent's own workspace wins; otherwise the most recently written file does; and the
    # choice is pinned for the session, so the reader never moves from one file to another under a stored position
    # (a pinned file that disappears is a lost position, not a reason to look elsewhere). A new session id forgets
    # the pin.
    def _main(self) -> Path | None:
        sid = self._session_source()
        if sid is None:
            return None
        if self._pinned is not None and self._pinned[0] == sid:
            return self._pinned[1]
        matches = sorted((self._home / "projects").glob(f"*/{sid}.jsonl"))
        if not matches:
            return None
        chosen = self._preferred(matches)
        self._pinned = (sid, chosen)
        return chosen

    def _preferred(self, matches: list[Path]) -> Path:
        if len(matches) == 1:
            return matches[0]
        if self._workspace is not None:
            slug = re.sub(r"[^A-Za-z0-9]", "-", str(self._workspace))
            own = [path for path in matches if path.parent.name == slug]
            if own:
                return own[0]
        return max(matches, key=lambda path: (path.stat().st_mtime_ns, str(path)))

    def _path(self, record: str) -> Path | None:
        main = self._main()
        if record == MAIN_RECORD or main is None:
            return main
        return main.parent / main.stem / "subagents" / f"agent-{record}.jsonl"

    async def position_before(self, record: str, wire_ids: Sequence[str]) -> str | None:
        return await asyncio.to_thread(self._position_before, record, tuple(wire_ids))

    # meiva:note
    # Where to start a record that already holds history: just before the first row that mentions one of the wire ids
    # (the first row of an input this process sent), or at the end of the last complete row when none does. Only
    # bytes are compared (a row that names the id anywhere), so the history is not decoded; the position is counted
    # the way a read counts it (every non-blank complete line is one row). A record with no complete row has no
    # position, and is read from its start.
    def _position_before(self, record: str, wire_ids: tuple[str, ...]) -> str | None:
        path = self._path(record)
        if path is None or not path.is_file():
            return None
        needles = tuple(wire.encode() for wire in wire_ids)
        offset = rows = start = end = 0
        last = b""
        with path.open("rb") as handle:
            for line in handle:
                if not line.endswith(b"\n") or any(needle in line for needle in needles):
                    break
                if line.strip():
                    rows += 1
                    start, end, last = offset, offset + len(line), line
                offset += len(line)
        if rows == 0:
            return None
        return encode(Position(start=start, offset=end, lines=rows, sha256=row_digest(last)))

    def _records(self) -> tuple[str, ...]:
        main = self._main()
        if main is None:
            return ()
        folder = main.parent / main.stem / "subagents"
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
