from __future__ import annotations

import asyncio
from pathlib import Path

from base.lm.harness.claude.reconciliation.transcript import transcript_path
from base.lm.harness.claude.transcript.parse import ParseState, parse_row
from base.lm.harness.claude.transcript.position import (
    START,
    Position,
    PositionLostError,
    advance,
    decode,
    encode,
    rows_after,
    verify,
)
from base.lm.harness.session.transcript.entries import MAIN_RECORD, AnyEntry
from base.lm.harness.session.transcript.ports import EntryBatch

# meiva:note
# The one reader of a Claude session's transcript: it implements `RecordReader` by reading the CLI's local
# JSONL after the stored position, whatever woke the writer (a stream frame, the `session_store` callback, a
# poll) and whether the process just started or has been running for hours. The main record is the session's
# file; a sub-agent's record is `<session>/subagents/agent-<id>.jsonl` next to it. The only memory it keeps is
# each record's parse state (the start of the open tool calls, for durations), which a restart loses: a result
# whose call was read before the restart then has no duration.
# meiva:link base/lm/harness/docs/transcript-projection.md


class ClaudeRecordReader:
    def __init__(self, *, claude_home: Path, session_id: str) -> None:
        self._home = claude_home
        self._session_id = session_id
        self._states: dict[str, ParseState] = {}

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
        data = self._bytes_after(path, position)
        rows = rows_after(position, data)
        if not rows:
            return None
        state = self._states.get(record, ParseState())
        entries: list[AnyEntry] = []
        for index, row in enumerate(rows):
            state, parsed = parse_row(state, row.row, line=position.lines + index)
            entries.extend(parsed)
        self._states[record] = state
        return EntryBatch(record, tuple(entries), encode(advance(position, rows)))

    @staticmethod
    def _bytes_after(path: Path, position: Position) -> bytes:
        with path.open("rb") as handle:
            if position.lines:
                _ = handle.seek(position.start)
                verify(position, handle.read(position.offset - position.start))
            _ = handle.seek(position.offset)
            return handle.read()
