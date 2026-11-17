from __future__ import annotations

from pathlib import Path

from base.lm.harness.antigravity.timing import timing_key
from base.lm.harness.antigravity.timing_store import load_timings, timing_path
from base.lm.harness.session.transcript.record_file import BLOCK_BYTES, Block, FileRow, RecordFile


class AntigravityRecordFile(RecordFile):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self._record = path
        self._timings = timing_path(path)

    def revision(self) -> int:
        try:
            if load_timings(self._record) is None:
                return 0
            return self._timings.stat().st_mtime_ns
        except OSError:
            return 0

    def rows_before(self, end: int, want: int = BLOCK_BYTES) -> Block:
        return self._enrich(super().rows_before(end, want))

    def rows_after(self, start: int) -> Block:
        return self._enrich(super().rows_after(start))

    def _enrich(self, block: Block) -> Block:
        data = load_timings(self._record)
        rows: list[FileRow] = []
        for file in block.rows:
            row = file.row
            key = timing_key(row.get("step_index"), row.get("created_at"), row.get("thinking"))
            value = None if data is None else data.entries.get(str(file.offset))
            ms = value.ms if value is not None and value.fingerprint == key else None
            rows.append(FileRow(file.offset, file.end, {**row, "meiva_reasoning_ms": ms}))
        return Block(block.start, block.end, tuple(rows))
