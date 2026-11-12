from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from base.lm.harness.claude.transcript.position import PositionLostError, decode
from base.lm.harness.claude.transcript.reader import ClaudeRecordReader
from base.lm.harness.claude.transcript.tests.support import (
    FIXTURES,
    entries_of,
    rows,
)
from base.lm.harness.session.transcript.entries import Anomaly, AnomalyKind, AnyEntry, ToolResult
from base.lm.harness.session.transcript.ports import EntryBatch

SYNC_AGENT = "af6798fb938fb227f"


def _session(scenario: str) -> str:
    session = rows(scenario)[0]["sessionId"]
    assert isinstance(session, str)
    return session


def _home(tmp_path: Path, scenario: str, *, copy_subagents: bool = False) -> tuple[Path, Path]:
    session = _session(scenario)
    folder = tmp_path / "projects" / "project"
    folder.mkdir(parents=True)
    target = folder / f"{session}.jsonl"
    _ = shutil.copyfile(FIXTURES / scenario / "main.jsonl", target)
    if copy_subagents:
        subagents = folder / session / "subagents"
        subagents.mkdir(parents=True)
        for source in (FIXTURES / scenario / "subagents").glob("*.jsonl"):
            _ = shutil.copyfile(source, subagents / f"agent-{source.name}")
    return tmp_path, target


def _keys(entries: list[AnyEntry]) -> list[str]:
    return [entry.key for entry in entries]


def _read(reader: ClaudeRecordReader, record: str, position: str | None):
    return asyncio.run(reader.read_after(record, position))


def test_one_read_returns_every_entry_the_parser_would_and_the_next_read_nothing(
    tmp_path: Path,
) -> None:
    home, _ = _home(tmp_path, "basic")
    reader = ClaudeRecordReader(claude_home=home, session_id=_session("basic"))
    batch = _read(reader, "main", None)
    assert batch is not None
    assert _keys(list(batch.entries)) == _keys(entries_of("basic"))
    assert decode(batch.position).lines == len(rows("basic"))
    assert _read(reader, "main", batch.position) is None


def test_reading_in_pieces_equals_reading_at_once(tmp_path: Path) -> None:
    home, target = _home(tmp_path, "steer")
    lines = target.read_text().splitlines(keepends=True)
    _ = target.write_text("".join(lines[:9]))
    reader = ClaudeRecordReader(claude_home=home, session_id=_session("steer"))
    first = _read(reader, "main", None)
    assert first is not None
    with target.open("a") as handle:
        _ = handle.write("".join(lines[9:15]))
        _ = handle.write(lines[15][: len(lines[15]) // 2])
    second = _read(reader, "main", first.position)
    assert second is not None and decode(second.position).lines == 15
    with target.open("a") as handle:
        _ = handle.write(lines[15][len(lines[15]) // 2 :])
        _ = handle.write("".join(lines[16:]))
    third = _read(reader, "main", second.position)
    assert third is not None
    pieces = [*first.entries, *second.entries, *third.entries]
    assert _keys(pieces) == _keys(entries_of("steer"))
    assert decode(third.position).lines == len(lines)


def test_a_new_reader_continues_from_the_stored_position_after_a_restart(tmp_path: Path) -> None:
    home, target = _home(tmp_path, "interrupt")
    lines = target.read_text().splitlines(keepends=True)
    _ = target.write_text("".join(lines[:12]))
    first = _read(
        ClaudeRecordReader(claude_home=home, session_id=_session("interrupt")), "main", None
    )
    assert first is not None
    _ = target.write_text("".join(lines))
    restarted = ClaudeRecordReader(claude_home=home, session_id=_session("interrupt"))
    rest = _read(restarted, "main", first.position)
    assert rest is not None
    assert _keys([*first.entries, *rest.entries]) == _keys(entries_of("interrupt"))
    assert rest.entries[0].order.seq >= decode(first.position).lines


def test_a_position_the_file_no_longer_holds_is_a_fault_not_a_restart(tmp_path: Path) -> None:
    home, target = _home(tmp_path, "basic")
    reader = ClaudeRecordReader(claude_home=home, session_id=_session("basic"))
    batch = _read(reader, "main", None)
    assert batch is not None
    lines = target.read_text().splitlines(keepends=True)
    _ = target.write_text("{}\n" + "".join(lines))
    with pytest.raises(PositionLostError):
        _ = _read(reader, "main", batch.position)
    _ = target.write_text("".join(lines[:3]))
    with pytest.raises(PositionLostError):
        _ = _read(reader, "main", batch.position)
    target.unlink()
    with pytest.raises(PositionLostError):
        _ = _read(reader, "main", batch.position)


def test_an_unreadable_stored_position_is_a_fault(tmp_path: Path) -> None:
    home, _ = _home(tmp_path, "basic")
    reader = ClaudeRecordReader(claude_home=home, session_id=_session("basic"))
    with pytest.raises(PositionLostError):
        _ = _read(reader, "main", "not a position")


def _append(target: Path, *lines: str | bytes) -> None:
    with target.open("ab") as handle:
        for line in lines:
            _ = handle.write(line if isinstance(line, bytes) else line.encode())


def _bad(entries: tuple[AnyEntry, ...]) -> list[str]:
    return [
        entry.detail
        for entry in entries
        if isinstance(entry, Anomaly) and entry.anomaly is AnomalyKind.BAD_ROW
    ]


def test_a_bad_row_is_skipped_with_an_anomaly_and_never_stops_the_rows_after_it(
    tmp_path: Path,
) -> None:
    home, target = _home(tmp_path, "basic")
    good = target.read_text().splitlines(keepends=True)
    deep = "[" * 200_000 + "]" * 200_000
    _ = target.write_text("")
    _append(
        target,
        "{not json}\n",
        "[1, 2]\n",
        b"\xff\xfe not utf-8\n",
        deep + "\n",
        "   \n",
        *good,
    )
    reader = ClaudeRecordReader(claude_home=home, session_id=_session("basic"))
    batch = _read(reader, "main", None)
    assert batch is not None
    assert _bad(batch.entries) == ["not_json", "not_object", "invalid_utf8", "too_deep"]
    good_keys = [key for key in _keys(list(batch.entries)) if "bad-row" not in key]
    assert good_keys == _keys(entries_of("basic"))
    assert decode(batch.position).lines == 4 + len(good)
    assert _read(reader, "main", batch.position) is None


def test_a_nul_in_tool_output_is_removed_so_the_row_can_be_stored(tmp_path: Path) -> None:
    home, target = _home(tmp_path, "basic")
    text = target.read_text()
    marker = '"content":"'
    assert marker in text
    _ = target.write_text(text.replace(marker, marker + "a\\u0000b", 1))
    reader = ClaudeRecordReader(claude_home=home, session_id=_session("basic"))
    batch = _read(reader, "main", None)
    assert batch is not None
    assert "\x00" not in repr(batch.entries) and "\\u0000" not in str(
        [row.row for row in batch.rows]
    )
    assert "\x00" not in str([row.row for row in batch.rows])


def test_a_last_row_without_a_uuid_is_still_checked_by_its_hash(tmp_path: Path) -> None:
    home, target = _home(tmp_path, "basic")
    _append(target, '{"type":"last-prompt","lastPrompt":"abc"}\n')
    reader = ClaudeRecordReader(claude_home=home, session_id=_session("basic"))
    batch = _read(reader, "main", None)
    assert batch is not None
    assert _read(reader, "main", batch.position) is None
    lines = target.read_text().splitlines(keepends=True)
    _ = target.write_text("".join(lines[:-1]) + '{"type":"last-prompt","lastPrompt":"abd"}\n')
    with pytest.raises(PositionLostError):
        _ = _read(reader, "main", batch.position)


def test_a_position_over_invalid_bytes_is_a_fault_not_an_uncaught_decode_error(
    tmp_path: Path,
) -> None:
    home, target = _home(tmp_path, "basic")
    reader = ClaudeRecordReader(claude_home=home, session_id=_session("basic"))
    batch = _read(reader, "main", None)
    assert batch is not None
    data = target.read_bytes()
    _ = target.write_bytes(data[:-3] + b"\xff\xfe\n")
    with pytest.raises(PositionLostError):
        _ = _read(reader, "main", batch.position)


def test_catching_up_on_a_large_file_is_a_series_of_bounded_batches(tmp_path: Path) -> None:
    home, target = _home(tmp_path, "steer")
    total = len(rows("steer"))
    reader = ClaudeRecordReader(
        claude_home=home, session_id=_session("steer"), max_batch_bytes=2_000
    )
    position: str | None = None
    seen: list[AnyEntry] = []
    batches = 0
    while (batch := _read(reader, "main", position)) is not None:
        batches += 1
        position = batch.position
        seen.extend(batch.entries)
        assert decode(position).offset <= 2_000 * batches + max(
            len(line) for line in target.read_bytes().splitlines()
        )
    assert batches > 3
    assert _keys(seen) == _keys(entries_of("steer"))
    assert decode(position or "").lines == total


def test_one_row_longer_than_a_batch_is_read_whole_and_one_longer_than_the_limit_is_skipped(
    tmp_path: Path,
) -> None:
    home, target = _home(tmp_path, "basic")
    reader = ClaudeRecordReader(
        claude_home=home,
        session_id=_session("basic"),
        max_batch_bytes=1_000,
        max_row_bytes=50_000,
    )

    def drain(position: str | None) -> tuple[str | None, list[EntryBatch]]:
        batches: list[EntryBatch] = []
        while (batch := _read(reader, "main", position)) is not None:
            batches.append(batch)
            position = batch.position
        return position, batches

    position, _ = drain(None)
    _append(target, '{"type":"x","pad":"' + "p" * 10_000 + '"}\n')
    _append(target, '{"type":"x","pad":"' + "p" * 90_000 + '"}\n')
    _append(target, '{"type":"y"}\n')
    position, batches = drain(position)
    assert len(batches) == 3
    assert [_bad(batch.entries) for batch in batches] == [[], ["oversize"], []]
    assert decode(position or "").offset == target.stat().st_size
    assert drain(position)[1] == []


def test_reading_again_from_the_same_position_after_a_failed_commit_gives_the_same_entries(
    tmp_path: Path,
) -> None:
    home, target = _home(tmp_path, "basic")
    lines = target.read_text().splitlines(keepends=True)
    split = next(i for i, line in enumerate(lines) if '"tool_result"' in line)
    _ = target.write_text("".join(lines[:split]))
    reader = ClaudeRecordReader(claude_home=home, session_id=_session("basic"))
    first = _read(reader, "main", None)
    assert first is not None
    _append(target, "".join(lines[split:]))
    attempt = _read(reader, "main", first.position)
    again = _read(reader, "main", first.position)
    assert attempt is not None and again is not None and attempt == again
    results = [e for e in again.entries if isinstance(e, ToolResult)]
    assert results and all(result.duration_ms is not None for result in results)
    rest = _read(reader, "main", again.position)
    assert rest is None


def test_the_sub_agent_files_are_records_read_the_same_way(tmp_path: Path) -> None:
    home, _ = _home(tmp_path, "subagents", copy_subagents=True)
    reader = ClaudeRecordReader(claude_home=home, session_id=_session("subagents"))
    assert asyncio.run(reader.records()) == ("main", "a0044dfe6d0ac4533", SYNC_AGENT)
    batch = _read(reader, SYNC_AGENT, None)
    assert batch is not None
    assert _keys(list(batch.entries)) == _keys(entries_of("subagents", SYNC_AGENT))
    assert {entry.record for entry in batch.entries} == {SYNC_AGENT}
    assert _read(reader, "not-there", None) is None


def test_a_session_with_no_file_yet_has_nothing_to_read(tmp_path: Path) -> None:
    reader = ClaudeRecordReader(claude_home=tmp_path, session_id="missing-session")
    assert asyncio.run(reader.records()) == ()
    assert _read(reader, "main", None) is None


def test_parsing_the_stored_rows_gives_the_entries_the_read_gave(tmp_path: Path) -> None:
    home, _ = _home(tmp_path, "subagents")
    reader = ClaudeRecordReader(claude_home=home, session_id=_session("subagents"))
    batch = _read(reader, "main", None)
    assert batch is not None and batch.rows
    again = ClaudeRecordReader(claude_home=home, session_id=_session("subagents"))
    rebuilt = again.parse_stored(batch.rows)
    assert _keys(list(rebuilt)) == _keys(list(batch.entries))
    assert [e.order.seq for e in rebuilt] == [e.order.seq for e in batch.entries]
