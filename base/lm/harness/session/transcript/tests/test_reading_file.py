from __future__ import annotations

import json
from pathlib import Path

import pytest
import zstandard

from base.lm.harness.session.transcript.record_file import (
    MAX_BLOCK_BYTES,
    RecordFile,
    RecordReadBudgetError,
    ZstdRecordFile,
)


def record(tmp_path: Path, raw: bytes, packed: bool) -> RecordFile:
    path = tmp_path / ("record.jsonl.zst" if packed else "record.jsonl")
    path.write_bytes(zstandard.ZstdCompressor().compress(raw) if packed else raw)
    return ZstdRecordFile(path) if packed else RecordFile(path)


def row(value: str) -> bytes:
    return (json.dumps({"value": value}) + "\n").encode()


@pytest.mark.parametrize("packed", [False, True])
def test_inclusive_row_larger_than_an_old_forward_radius_is_complete(
    tmp_path: Path, packed: bool
) -> None:
    prefix, target, following = row("before"), row("x" * (9 * 1024 * 1024)), row("after")
    raw = prefix + target + following + b'{"partial":'
    file = record(tmp_path, raw, packed)
    file.prepare_reading(len(prefix))
    found = file.row_at(len(prefix))
    assert found is not None and found.end == len(prefix) + len(target)
    assert len(str(found.row["value"])) == 9 * 1024 * 1024
    newer = file.rows_forward(found.end)
    assert newer.rows[0].row["value"] == "after"
    assert file.complete_record_end() == len(raw) - len(b'{"partial":')
    if isinstance(file, ZstdRecordFile):
        assert file.bytes_read == len(raw)
        assert file.held_bytes <= MAX_BLOCK_BYTES
        assert file.stream_passes == 1
        assert file.compressed_bytes_read == (tmp_path / "record.jsonl.zst").stat().st_size
    else:
        assert file.bytes_read <= MAX_BLOCK_BYTES


@pytest.mark.parametrize("packed", [False, True])
@pytest.mark.parametrize("offset", [1, 1000])
def test_inside_row_and_outside_source_are_not_qualified(
    tmp_path: Path, packed: bool, offset: int
) -> None:
    file = record(tmp_path, row("committed"), packed)
    file.prepare_reading(offset)
    assert file.row_at(offset) is None


@pytest.mark.parametrize("packed", [False, True])
def test_partial_target_is_not_a_complete_row(tmp_path: Path, packed: bool) -> None:
    prefix = row("committed")
    file = record(tmp_path, prefix + b'{"value":"unfinished"}', packed)
    file.prepare_reading(len(prefix))
    assert file.row_at(len(prefix)) is None
    assert file.complete_record_end() == len(prefix)


@pytest.mark.parametrize("packed", [False, True])
def test_over_budget_target_is_explicit_and_plain_reads_stay_bounded(
    tmp_path: Path, packed: bool
) -> None:
    file = record(tmp_path, row("x" * (MAX_BLOCK_BYTES + 1)), packed)
    with pytest.raises(RecordReadBudgetError):
        file.prepare_reading(0)
        _ = file.row_at(0)
    if not isinstance(file, ZstdRecordFile):
        assert file.bytes_read <= MAX_BLOCK_BYTES


def test_compressed_middle_window_uses_one_stream_and_keeps_both_sides(tmp_path: Path) -> None:
    line = row("x" * 1024)
    raw = line * 20000
    file = record(tmp_path, raw, True)
    offset = len(line) * 10000
    file.prepare_reading(offset)
    found = file.row_at(offset)
    assert found is not None
    assert file.rows_before(offset).rows
    assert file.rows_forward(found.end).rows
    assert file.bytes_read == len(raw)
    assert isinstance(file, ZstdRecordFile) and file.held_bytes <= MAX_BLOCK_BYTES
    assert file.stream_passes == 1
    assert file.compressed_bytes_read == (tmp_path / "record.jsonl.zst").stat().st_size


def test_quote_of_first_compressed_row_stops_before_replaying_archive(tmp_path: Path) -> None:
    first = row("selected")
    raw = first + row("x" * 1024) * (MAX_BLOCK_BYTES // 512)
    file = record(tmp_path, raw, True)
    with file.quote_row(0, inbound=False) as found:
        assert found is not None and found.row.row["value"] == "selected"
        assert not found.is_tail()
    assert file.bytes_read <= 64 * 1024
    assert isinstance(file, ZstdRecordFile) and file.held_bytes == 0
    assert file.stream_passes == 1


def test_compressed_quote_refuses_a_row_beyond_the_stream_budget(tmp_path: Path) -> None:
    line = row("x" * 1024)
    raw = line * (MAX_BLOCK_BYTES // len(line) + 1)
    file = record(tmp_path, raw, True)
    with pytest.raises(RecordReadBudgetError), file.quote_row(len(raw) - len(line), inbound=False):
        pytest.fail("the bounded quote read cannot reach the complete row")
    assert file.bytes_read <= MAX_BLOCK_BYTES
