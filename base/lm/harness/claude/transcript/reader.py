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
from base.lm.harness.claude.transcript.parse import ParseState, parse_row, row_version
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
from base.lm.harness.claude.transcript.scrub import scrub
from base.lm.harness.claude.transcript.store_filter import stored_row
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
# What a read costs when nothing is new: one `stat` of the file. The reader remembers, per record, the size and
# modification time at which a read from the stored position found no complete row after it (after the hash of the
# last row read had been checked), and answers "nothing new" without opening the file while both are unchanged; so a
# poll of an idle session or of hundreds of finished sub-agents opens nothing and hashes nothing. A file that is
# longer or touched is read, with the last row checked as always.
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
_GLOB_EVERY_MISSES = 20
_QUIET_KEPT = 4_096
_STATES_KEPT = 256
_LISTING_SETTLE_NS = 2_000_000_000


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
        now_ns: Callable[[], int],
    ) -> None:
        self._now_ns = now_ns
        self._home = claude_home
        self._session_source = (lambda: session_id) if isinstance(session_id, str) else session_id
        self._workspace = workspace
        self._pinned: tuple[str, Path] | None = None
        self._max_batch = max_batch_bytes
        self._max_row = max_row_bytes
        self._states: dict[str, dict[str, ParseState]] = {}
        self._stored_states: dict[str, ParseState] = {}
        self._quiet: dict[str, tuple[str | None, tuple[int, int]]] = {}
        self._listing: tuple[int, tuple[str, ...], int] | None = None
        self._misses = 0
        self._misses_session: str | None = None
        self._cli_version: str | None = None
        self.bytes_read = 0

    @classmethod
    def for_session(
        cls,
        *,
        environ: Mapping[str, str],
        workspace: Path,
        session_id: Callable[[], str | None],
        now_ns: Callable[[], int],
    ) -> ClaudeRecordReader:
        return cls(
            claude_home=resolve_claude_home(None, environ),
            session_id=session_id,
            workspace=workspace,
            now_ns=now_ns,
        )

    @property
    def cli_version(self) -> str | None:
        return self._cli_version

    def wire_id(self, key: str) -> str:
        return key_uuid(key)

    def parse_stored(self, rows: Sequence[StoredRow]) -> tuple[AnyEntry, ...]:
        entries: list[AnyEntry] = []
        for stored in rows:
            state = self._stored_states.pop(stored.record, ParseState())
            state, parsed = parse_row(state, stored.row, line=stored.ordinal)
            _remember(self._stored_states, stored.record, state)
            entries.extend(parsed)
        return tuple(entries)

    async def records(self, *, exhaustive: bool = False) -> tuple[str, ...]:
        return await asyncio.to_thread(self._records, exhaustive=exhaustive)

    async def read_after(self, record: str, position: str | None) -> EntryBatch | None:
        return await asyncio.to_thread(self._read, record, position)

    # meiva:note
    # The session's own file, `projects/<project>/<session id>.jsonl`. The session id is unique, but the CLI can
    # leave the same one under several project directories (a resume from another working directory copies the
    # file). The directory of the agent's own workspace wins and is looked for by its path (one stat); only when it is
    # not there are all the project directories searched, and that search is made at the first miss and at every
    # `_GLOB_EVERY_MISSES`-th after it, not at every poll of a session whose file has not appeared. Then the most
    # recently written file wins; and the choice is pinned for the session, so the reader never moves from one file to
    # another under a stored position (a pinned file that disappears is a lost position, not a reason to look
    # elsewhere). A new session id forgets the pin and starts the count of misses again, so the first poll of a session
    # always searches; `exhaustive` (the last read before a close, the reads that must not miss a file) searches at every miss.
    def _main(self, *, exhaustive: bool = False) -> Path | None:
        sid = self._session_source()
        if sid is None:
            return None
        if self._pinned is not None and self._pinned[0] == sid:
            return self._pinned[1]
        own = self._own(sid)
        if own is not None and own.is_file():
            self._pinned = (sid, own)
            return own
        if self._misses_session != sid:
            self._misses_session, self._misses = sid, 0
        self._misses += 1
        if not exhaustive and self._misses % _GLOB_EVERY_MISSES != 1:
            return None
        matches = sorted((self._home / "projects").glob(f"*/{sid}.jsonl"))
        if not matches:
            return None
        chosen = self._preferred(matches)
        self._pinned = (sid, chosen)
        return chosen

    def _own(self, sid: str) -> Path | None:
        if self._workspace is None:
            return None
        slug = re.sub(r"[^A-Za-z0-9]", "-", str(self._workspace))
        return self._home / "projects" / slug / f"{sid}.jsonl"

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

    async def position_before(
        self, record: str, wire_ids: Sequence[str], *, after_ordinal: int = -1
    ) -> str | None:
        return await asyncio.to_thread(
            self._position_before, record, tuple(wire_ids), after_ordinal
        )

    # meiva:note
    # Where to start a record that already holds history: just before the first row that mentions one of the wire ids
    # (the first row of an input this process sent), or at the end of the last complete row when none does. Only
    # bytes are compared (a row that names the id anywhere), so the history is not decoded; the position is counted
    # the way a read counts it (every non-blank complete line is one row). A record with no complete row has no
    # position, and is read from its start. `after_ordinal` is the highest ordinal already stored for the record: the
    # position counts its rows from above it (the count only numbers the rows that follow, so it is free to be larger
    # than the file's rows), which keeps the rows of a rewritten file from taking the ordinals of the stored ones.
    def _position_before(
        self, record: str, wire_ids: tuple[str, ...], after_ordinal: int
    ) -> str | None:
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
        return encode(
            Position(
                start=start, offset=end, lines=max(rows, after_ordinal + 1), sha256=row_digest(last)
            )
        )

    # meiva:note
    # The records of the session: the main one and one per sub-agent file. The folder is listed only when its modification
    # time has changed (a file was added) or the listing was made too soon after it to be trusted: a directory's
    # modification time is a clock tick, and a second file created in the same tick leaves it where it was, so a listing
    # made less than `_LISTING_SETTLE_NS` after the stamp it saw is made again at the next poll, until one is made after
    # the folder had been quiet for that long (that listing could not have missed a file of the same tick). Between changes
    # the answer is the one remembered, so a session with hundreds of finished sub-agents costs one stat per poll for
    # its listing.
    def _records(self, *, exhaustive: bool = False) -> tuple[str, ...]:
        main = self._main(exhaustive=exhaustive)
        if main is None:
            return ()
        folder = main.parent / main.stem / "subagents"
        try:
            stamp = folder.stat().st_mtime_ns
        except FileNotFoundError:
            self._listing = None
            return (MAIN_RECORD,)
        if (
            self._listing is None
            or self._listing[0] != stamp
            or self._listing[2] - stamp < _LISTING_SETTLE_NS
        ):
            children = tuple(
                sorted(
                    path.name.removeprefix("agent-").removesuffix(".jsonl")
                    for path in folder.glob("agent-*.jsonl")
                )
            )
            self._listing = (stamp, children, self._now_ns())
        return (MAIN_RECORD, *self._listing[1])

    def _read(self, record: str, stored: str | None) -> EntryBatch | None:
        position = START if stored is None else decode(stored)
        found = self._stat(record, position)
        if found is None:
            return None
        path, stamp = found
        if self._quiet.get(record) == (stored, stamp):
            return None
        with path.open("rb") as handle:
            self._verify(handle, position)
            chunk = self._next_chunk(handle, position)
        rows = (*rows_after(position, chunk.data), *((chunk.skipped,) if chunk.skipped else ()))
        if not rows:
            self._remember_quiet(record, stored, stamp)
            return None
        return self._batch(record, stored, position, rows, chunk)

    def _stat(self, record: str, position: Position) -> tuple[Path, tuple[int, int]] | None:
        path = self._path(record)
        try:
            status = path.stat() if path is not None else None
        except FileNotFoundError:
            status = None
        if path is None or status is None or not path.is_file():
            if position.lines:
                raise PositionLostError(f"the transcript of record {record!r} is gone")
            return None
        return path, (status.st_size, status.st_mtime_ns)

    def _remember_quiet(self, record: str, stored: str | None, stamp: tuple[int, int]) -> None:
        if len(self._quiet) >= _QUIET_KEPT:
            self._quiet.clear()
        self._quiet[record] = (stored, stamp)

    def _batch(
        self,
        record: str,
        stored: str | None,
        position: Position,
        rows: Sequence[RawRow],
        chunk: _Chunk,
    ) -> EntryBatch:
        cache = self._states.pop(record, {})
        self._states[record] = cache
        while len(self._states) > _STATES_KEPT:
            del self._states[next(iter(self._states))]
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
        return EntryBatch(record, tuple(entries), moved, tuple(kept), len(chunk.data))

    def _row(
        self, record: str, ordinal: int, row: RawRow, state: ParseState
    ) -> tuple[ParseState, tuple[AnyEntry, ...], dict[str, object] | None]:
        if row.row is None:
            return state, (_bad_row(record, ordinal, row.bad),), None
        try:
            _ = scrub(row.row)
            self._cli_version = row_version(row.row) or self._cli_version
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


def _remember(states: dict[str, ParseState], record: str, state: ParseState) -> None:
    states[record] = state
    while len(states) > _STATES_KEPT:
        del states[next(iter(states))]


def _bad_row(record: str, ordinal: int, reason: str | None) -> Anomaly:
    detail = reason or "unusable"
    return Anomaly(
        key=f"claude:anomaly:bad-row:{record}:{ordinal}",
        order=Order(ordinal),
        record=record,
        anomaly=AnomalyKind.BAD_ROW,
        detail=detail,
    )
