from __future__ import annotations

from pathlib import Path

from base.lm.harness.antigravity.timing import timing_key
from base.lm.harness.antigravity.timing_store import TimingRecord, load_timings, timing_path
from base.lm.harness.antigravity.transcript.input_store import (
    ROW_KEY,
    inputs_path,
    load_ledger,
    resolve_inputs,
    unbound_inputs,
)
from base.lm.harness.session.transcript.record_file import BLOCK_BYTES, Block, FileRow, RecordFile


class AntigravityRecordFile(RecordFile):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self._record = path
        self._timings = timing_path(path)
        self._inputs = inputs_path(path)
        self._bound: dict[int, str] = {}
        self._bound_signature: tuple[int, ...] | None = None
        self._loaded = False
        self._signature: tuple[int, ...] | None = None
        self._data: TimingRecord | None = None

    def revision(self) -> int:
        data = self._load()
        if data is None or self._signature is None:
            return 0
        timing_mtime_ns, timing_ino, timing_size = self._signature[:3]
        return hash((timing_mtime_ns, timing_ino, timing_size, len(data.entries))) or 1

    def watch_signature(self) -> tuple[int, ...] | None:
        base = super().watch_signature()
        if base is None:
            return None
        try:
            timing = self._timings.stat()
        except OSError:
            timing_part = (0, 0, 0)
        else:
            timing_part = (timing.st_ino, timing.st_size, timing.st_mtime_ns)
        return (*base, *timing_part, *self._inputs_signature())

    def _inputs_signature(self) -> tuple[int, ...]:
        try:
            stat = self._inputs.stat()
        except OSError:
            return (0, 0, 0)
        return (stat.st_ino, stat.st_size, stat.st_mtime_ns)

    def _bindings(self) -> dict[int, str]:
        record = super().watch_signature()
        if record is None:
            return {}
        signature = (*record, *self._inputs_signature())
        if signature != self._bound_signature:
            ledger = load_ledger(self._inputs)
            unbound = unbound_inputs(ledger)
            rows = (
                super().rows_after(min(sent.min_offset for sent in unbound)).rows if unbound else ()
            )
            self._bound = resolve_inputs(((row.offset, row.row) for row in rows), ledger)
            self._bound_signature = signature
        return self._bound

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
            self._data = load_timings(self._record, allow_growth=True)
            self._loaded = True
        return self._data

    def row_at(self, offset: int) -> FileRow | None:
        row = super().row_at(offset)
        return self._enrich(Block(row.offset, row.end, (row,))).rows[0] if row else None

    def rows_before(self, end: int, want: int = BLOCK_BYTES) -> Block:
        return self._enrich(super().rows_before(end, want))

    def rows_after(self, start: int) -> Block:
        return self._enrich(super().rows_after(start))

    def rows_forward(self, start: int, want: int = BLOCK_BYTES) -> Block:
        return self._enrich(super().rows_forward(start, want))

    def _enrich(self, block: Block) -> Block:
        data = self._load()
        bound = self._bindings()
        rows: list[FileRow] = []
        for file in block.rows:
            row = file.row
            key = timing_key(row.get("step_index"), row.get("created_at"), row.get("thinking"))
            value = (
                None
                if data is None or file.offset >= data.size
                else data.entries.get(str(file.offset))
            )
            ms = value.ms if value is not None and value.fingerprint == key else None
            key = bound.get(file.offset)
            enriched = {**row, "meiva_reasoning_ms": ms}
            rows.append(
                FileRow(
                    file.offset, file.end, enriched if key is None else {**enriched, ROW_KEY: key}
                )
            )
        return Block(block.start, block.end, tuple(rows))
