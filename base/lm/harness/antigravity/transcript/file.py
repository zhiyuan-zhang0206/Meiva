from __future__ import annotations

from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

from base.lm.harness.antigravity.timing import timing_key
from base.lm.harness.antigravity.timing_store import TimingRecord, load_timings, timing_path
from base.lm.harness.antigravity.transcript.input_store import (
    MAX_SCAN_BYTES,
    ROW_KEY,
    Ledger,
    SentInput,
    answers,
    inputs_path,
    load_ledger,
    matching_pending_inputs,
    resolve_unbound,
    unbound_inputs,
)
from base.lm.harness.antigravity.transcript.text import legacy_marker_digest
from base.lm.harness.session.transcript.record_file import (
    BLOCK_BYTES,
    Block,
    FileRow,
    QuoteRow,
    RecordFile,
    RecordReadBudgetError,
)

# meiva:link base/lm/harness/docs/transcript-projection.md
_QUOTE_LEDGER_BYTES = BLOCK_BYTES
_QUOTE_MATCH_CHECKS = 4096


class AntigravityRecordFile(RecordFile):
    def __init__(self, path: Path, scan_bytes: int = MAX_SCAN_BYTES) -> None:
        super().__init__(path)
        self._scan_bytes = scan_bytes
        self._record = path
        self._timings = timing_path(path)
        self._inputs = inputs_path(path)
        self._ledger_data: Ledger | None = None
        self._ledger_signature: tuple[int, ...] | None = None
        self._scan: Mapping[int, str] = {}
        self._scan_signature: tuple[int, ...] | None = None
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

    def _ledger(self) -> Ledger:
        signature = self._inputs_signature()
        if signature != self._ledger_signature or self._ledger_data is None:
            self._ledger_data, self._ledger_signature = load_ledger(self._inputs), signature
        return self._ledger_data

    def _bindings(self, block: Block) -> Mapping[int, str]:
        ledger = self._ledger()
        unbound = unbound_inputs(ledger)
        takes = (
            file.offset not in ledger.bound
            and any(file.offset >= sent.min_offset and answers(file.row, sent) for sent in unbound)
            for file in block.rows
        )
        return self._scanned(ledger, unbound) if any(takes) else ledger.bound

    def _scanned(self, ledger: Ledger, unbound: Sequence[SentInput]) -> Mapping[int, str]:
        signature = self.watch_signature()
        if signature is None or signature != self._scan_signature:
            floor = self.size() - self._scan_bytes
            near = [sent for sent in unbound if sent.min_offset >= floor]
            rows = (
                RecordFile(self._record).rows_after(min(s.min_offset for s in near)).rows
                if near
                else ()
            )
            found = [(row.offset, row.row) for row in rows]
            self._scan, self._scan_signature = resolve_unbound(found, ledger.bound, near), signature
        return self._scan

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

    @contextmanager
    def quote_row(self, offset: int, *, inbound: bool) -> Generator[QuoteRow | None]:
        self.prepare_reading(offset)
        row = super().row_at(offset)
        if row is None:
            yield None
            return
        key = self._quote_input_key(row) if inbound else None
        if key is not None:
            row = FileRow(row.offset, row.end, {**row.row, ROW_KEY: key})
        yield QuoteRow(row, lambda: row.end == self.complete_record_end())

    def _quote_input_key(self, file: FileRow) -> str | None:
        content = file.row.get("content")
        if file.row.get("type") != "USER_INPUT" or not isinstance(content, str):
            return None
        ledger = load_ledger(self._inputs, max_bytes=_QUOTE_LEDGER_BYTES)
        if file.offset in ledger.bound:
            return ledger.bound[file.offset]
        if legacy_marker_digest(content) is not None:
            return None
        unbound = unbound_inputs(ledger)
        candidates = matching_pending_inputs(file, unbound)
        if not candidates:
            return None
        return self._quote_reconcile(file, ledger, unbound, candidates)

    def _quote_reconcile(
        self,
        file: FileRow,
        ledger: Ledger,
        unbound: Sequence[SentInput],
        candidates: Sequence[SentInput],
    ) -> str | None:
        scan_bytes = min(self._scan_bytes, MAX_SCAN_BYTES)
        floor = self.size() - scan_bytes
        if any(sent.min_offset < floor for sent in candidates):
            raise RecordReadBudgetError(
                "The relevant input predates the quote reconciliation window"
            )
        near = [sent for sent in unbound if sent.min_offset >= floor]
        start = min(sent.min_offset for sent in near)
        raw = RecordFile(self._record).rows_forward(start, min(scan_bytes, file.end - start))
        if raw.end < file.end:
            raise RecordReadBudgetError("The target row exceeds the quote reconciliation window")
        rows = [(row.offset, row.row) for row in raw.rows]
        return resolve_unbound(rows, ledger.bound, near, max_checks=_QUOTE_MATCH_CHECKS).get(
            file.offset
        )

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
        bound = self._bindings(block)
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
