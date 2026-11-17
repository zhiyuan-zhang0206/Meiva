from __future__ import annotations

from pathlib import Path

from base.lm.harness.antigravity.timing import timing_key
from base.lm.harness.antigravity.timing_store import TimingRecord, load_timings, timing_path
from base.lm.harness.session.transcript.record_file import BLOCK_BYTES, Block, FileRow, RecordFile


class AntigravityRecordFile(RecordFile):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self._record = path
        self._timings = timing_path(path)
        self._loaded = False
        self._signature: tuple[int, ...] | None = None
        self._data: TimingRecord | None = None

    def revision(self) -> int:
        data = self._load()
        return self._signature[0] if data is not None and self._signature is not None else 0

    def _load(self) -> TimingRecord | None:
        try:
            timing, raw = self._timings.stat(), self._record.stat()
            signature = (
                timing.st_mtime_ns,
                timing.st_ino,
                timing.st_size,
                raw.st_mtime_ns,
                raw.st_ino,
                raw.st_size,
            )
        except OSError:
            signature = None
        if not self._loaded or signature != self._signature:
            self._signature = signature
            self._data = load_timings(self._record)
            self._loaded = True
        return self._data

    def rows_before(self, end: int, want: int = BLOCK_BYTES) -> Block:
        return self._enrich(super().rows_before(end, want))

    def rows_after(self, start: int) -> Block:
        return self._enrich(super().rows_after(start))

    def _enrich(self, block: Block) -> Block:
        data = self._load()
        rows: list[FileRow] = []
        for file in block.rows:
            row = file.row
            key = timing_key(row.get("step_index"), row.get("created_at"), row.get("thinking"))
            value = None if data is None else data.entries.get(str(file.offset))
            ms = value.ms if value is not None and value.fingerprint == key else None
            rows.append(FileRow(file.offset, file.end, {**row, "meiva_reasoning_ms": ms}))
        return Block(block.start, block.end, tuple(rows))
