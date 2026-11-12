from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from base.lm.harness.claude.transcript.position import (
    BadRowError,
    PositionLostError,
    decode,
)
from base.lm.harness.claude.transcript.reader import ClaudeRecordReader
from base.lm.harness.claude.transcript.tests.support import (
    FIXTURES,
    entries_of,
    rows,
)
from base.lm.harness.session.transcript.entries import AnyEntry

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


def test_a_complete_row_that_is_not_json_is_an_error_not_a_skip(tmp_path: Path) -> None:
    home, target = _home(tmp_path, "basic")
    _ = target.write_text(target.read_text() + "{not json}\n")
    reader = ClaudeRecordReader(claude_home=home, session_id=_session("basic"))
    with pytest.raises(BadRowError):
        _ = _read(reader, "main", None)


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
