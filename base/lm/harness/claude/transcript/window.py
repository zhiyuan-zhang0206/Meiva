from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from base.lm.harness.claude.delivery import key_uuid
from base.lm.harness.claude.reconciliation.transcript import claude_home
from base.lm.harness.claude.transcript.parse import ParseState, parse_row
from base.lm.harness.claude.transcript.position import Position, rows_after
from base.lm.harness.claude.transcript.scrub import scrub
from base.lm.harness.session.transcript.entries import MAIN_RECORD, AnyEntry

# meiva:note
# The Claude half of reading a session's timeline at the moment it is asked for: where the main record
# lives, which bytes of it to read and what its rows mean, and nothing about what a timeline shows. A
# row's identity is its byte offset in the file (the file only grows, so an offset is stable, ordered and
# the same however the row was reached), and the offset is also the `seq` of the entries the parser reads
# from it. Reads go backwards from a line boundary, one block at a time, so what a request reads follows
# the size of the window it asks for and not the size of the file. A row that cannot be decoded is
# skipped: the file is the CLI's own and its rows are never repaired here. Sub-agent files are not read.
# meiva:link base/lm/harness/docs/transcript-projection.md

BLOCK_BYTES = 256 * 1024
MAX_BLOCK_BYTES = 16 * 1024 * 1024
_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class FileRow:
    offset: int
    end: int
    row: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class Block:
    start: int
    end: int
    rows: tuple[FileRow, ...]


def wire_id(key: str) -> str:
    return key_uuid(key)


# meiva:note
# Where the session's file is looked for, for a message that says so: the session's project directory under its own
# Claude home (every other project directory is searched after it).
def record_location(environ: Mapping[str, str], workspace: Path) -> str:
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(workspace))
    return str(claude_home(None, environ) / "projects" / slug)


# meiva:note
# The record file of a session as the session itself finds it: the Claude home is the one the session's own
# environment names (`$HOME/.claude`, or the configured directory), the working directory is the session's.
def session_file(environ: Mapping[str, str], workspace: Path, session_id: str) -> Path | None:
    return transcript_path(claude_home(None, environ), workspace, session_id)


# meiva:note
# The project directory name the CLI derives from the working directory, and the file the reader of the
# live path uses first; the session's id is unique, so a search of every project directory is the fallback
# when the CLI filed it under another name.
def transcript_path(claude_home: Path, workspace: Path, session_id: str) -> Path | None:
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(workspace))
    own = claude_home / "projects" / slug / f"{session_id}.jsonl"
    if own.is_file():
        return own
    matches = sorted((claude_home / "projects").glob(f"*/{session_id}.jsonl"))
    if not matches:
        return None
    own_dir = [path for path in matches if path.parent.name == slug]
    if own_dir:
        return own_dir[0]
    return max(matches, key=lambda path: (path.stat().st_mtime_ns, str(path)))


# meiva:note
# One transcript file, read in bounded pieces, counting the bytes it reads (`bytes_read`) so a test or a gauge
# can state what a request cost.
class RecordFile:
    def __init__(self, path: Path) -> None:
        self._path = path
        self.bytes_read = 0

    def size(self) -> int:
        return self._path.stat().st_size

    # meiva:note
    # The end of the last complete line: a row the CLI is still writing is not read.
    def complete_end(self) -> int:
        with self._path.open("rb") as handle:
            position = handle.seek(0, 2)
            while position > 0:
                low = max(0, position - _CHUNK_BYTES)
                _ = handle.seek(low)
                data = handle.read(position - low)
                self.bytes_read += len(data)
                cut = data.rfind(b"\n")
                if cut >= 0:
                    return low + cut + 1
                position = low
        return 0

    # meiva:note
    # The rows in the block of about `want` bytes that ends at `end` (a line boundary): the block starts at the
    # first line boundary inside it, widened while one row is longer than the block. `start == 0` means
    # the file's first row is in it.
    def rows_before(self, end: int, want: int = BLOCK_BYTES) -> Block:
        low = max(0, end - want)
        while True:
            data = self._read(max(0, low - 1), end)
            first = max(0, low - 1)
            if low == 0:
                start = 0
            elif data[:1] == b"\n":
                data, start = data[1:], low
            else:
                cut = data.find(b"\n")
                if cut < 0:
                    if low == 0 or end - low >= MAX_BLOCK_BYTES:
                        return Block(end, end, ())
                    low = max(0, low - max(want, end - low))
                    continue
                data, start = data[cut + 1 :], first + cut + 1
            break
        rows = tuple(
            FileRow(raw.start, raw.end, _scrubbed(raw.row))
            for raw in rows_after(Position(start=0, offset=start, lines=0, sha256=""), data)
            if raw.row is not None
        )
        return Block(start, end, rows)

    def _read(self, low: int, end: int) -> bytes:
        with self._path.open("rb") as handle:
            _ = handle.seek(low)
            data = handle.read(end - low)
        self.bytes_read += len(data)
        return data

    def rows_after(self, start: int) -> Block:
        end = self.complete_end()
        if end <= start:
            return Block(start, start, ())
        data = self._read(start, end)
        rows = tuple(
            FileRow(raw.start, raw.end, _scrubbed(raw.row))
            for raw in rows_after(Position(start=0, offset=start, lines=0, sha256=""), data)
            if raw.row is not None
        )
        return Block(start, end, rows)


def _scrubbed(row: Mapping[str, object] | None) -> Mapping[str, object]:
    if row is None:
        raise ValueError("a bad row has no content")
    _ = scrub(row)
    return row


# meiva:note
# Rows read into entries, in file order, each entry named by its row's offset. The parser's only memory is when each
# open tool call started, for the call's duration, so a parser that is given the rows of one read after another
# (a live read) and one given them all at once (a cold read) agree. A row of another record (a sub-agent's row
# that the main file should not hold) yields nothing.
class EntryParser:
    def __init__(self) -> None:
        self._state = ParseState()

    def entries(self, rows: Sequence[FileRow]) -> tuple[AnyEntry, ...]:
        found: list[AnyEntry] = []
        for file_row in rows:
            self._state, parsed = parse_row(self._state, file_row.row, line=file_row.offset)
            found.extend(entry for entry in parsed if entry.record == MAIN_RECORD)
        return tuple(found)
