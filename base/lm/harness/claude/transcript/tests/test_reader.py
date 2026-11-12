from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path

import pytest

from base.lm.harness.claude.transcript.parse import ParseState, parse_row
from base.lm.harness.claude.transcript.position import PositionLostError, decode
from base.lm.harness.claude.transcript.reader import ClaudeRecordReader
from base.lm.harness.claude.transcript.tests.support import (
    FIXTURES,
    entries_of,
    now_ns,
    rows,
)
from base.lm.harness.session.transcript.entries import (
    Anomaly,
    AnomalyKind,
    AnyEntry,
    HumanInput,
    ToolResult,
)
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


def rows_text(scenario: str) -> str:
    return (FIXTURES / scenario / "main.jsonl").read_text()


def _keys(entries: list[AnyEntry]) -> list[str]:
    return [entry.key for entry in entries]


def _read(reader: ClaudeRecordReader, record: str, position: str | None):
    return asyncio.run(reader.read_after(record, position))


def test_one_read_returns_every_entry_the_parser_would_and_the_next_read_nothing(
    tmp_path: Path,
) -> None:
    home, _ = _home(tmp_path, "basic")
    reader = ClaudeRecordReader(now_ns=now_ns, claude_home=home, session_id=_session("basic"))
    batch = _read(reader, "main", None)
    assert batch is not None
    assert _keys(list(batch.entries)) == _keys(entries_of("basic"))
    assert decode(batch.position).lines == len(rows("basic"))
    assert _read(reader, "main", batch.position) is None


def test_reading_in_pieces_equals_reading_at_once(tmp_path: Path) -> None:
    home, target = _home(tmp_path, "steer")
    lines = target.read_text().splitlines(keepends=True)
    _ = target.write_text("".join(lines[:9]))
    reader = ClaudeRecordReader(now_ns=now_ns, claude_home=home, session_id=_session("steer"))
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
        ClaudeRecordReader(now_ns=now_ns, claude_home=home, session_id=_session("interrupt")),
        "main",
        None,
    )
    assert first is not None
    _ = target.write_text("".join(lines))
    restarted = ClaudeRecordReader(
        now_ns=now_ns, claude_home=home, session_id=_session("interrupt")
    )
    rest = _read(restarted, "main", first.position)
    assert rest is not None
    assert _keys([*first.entries, *rest.entries]) == _keys(entries_of("interrupt"))
    assert rest.entries[0].order.seq >= decode(first.position).lines


def test_a_position_the_file_no_longer_holds_is_a_fault_not_a_restart(tmp_path: Path) -> None:
    home, target = _home(tmp_path, "basic")
    reader = ClaudeRecordReader(now_ns=now_ns, claude_home=home, session_id=_session("basic"))
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
    reader = ClaudeRecordReader(now_ns=now_ns, claude_home=home, session_id=_session("basic"))
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
    reader = ClaudeRecordReader(now_ns=now_ns, claude_home=home, session_id=_session("basic"))
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
    reader = ClaudeRecordReader(now_ns=now_ns, claude_home=home, session_id=_session("basic"))
    batch = _read(reader, "main", None)
    assert batch is not None
    assert "\x00" not in repr(batch.entries) and "\\u0000" not in str(
        [row.row for row in batch.rows]
    )
    assert "\x00" not in str([row.row for row in batch.rows])


def test_a_last_row_without_a_uuid_is_still_checked_by_its_hash(tmp_path: Path) -> None:
    home, target = _home(tmp_path, "basic")
    _append(target, '{"type":"last-prompt","lastPrompt":"abc"}\n')
    reader = ClaudeRecordReader(now_ns=now_ns, claude_home=home, session_id=_session("basic"))
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
    reader = ClaudeRecordReader(now_ns=now_ns, claude_home=home, session_id=_session("basic"))
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
        now_ns=now_ns, claude_home=home, session_id=_session("steer"), max_batch_bytes=2_000
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
        now_ns=now_ns,
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
    reader = ClaudeRecordReader(now_ns=now_ns, claude_home=home, session_id=_session("basic"))
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
    reader = ClaudeRecordReader(now_ns=now_ns, claude_home=home, session_id=_session("subagents"))
    assert asyncio.run(reader.records()) == ("main", "a0044dfe6d0ac4533", SYNC_AGENT)
    batch = _read(reader, SYNC_AGENT, None)
    assert batch is not None
    assert _keys(list(batch.entries)) == _keys(entries_of("subagents", SYNC_AGENT))
    assert {entry.record for entry in batch.entries} == {SYNC_AGENT}
    assert _read(reader, "not-there", None) is None


def test_a_session_with_no_file_yet_has_nothing_to_read(tmp_path: Path) -> None:
    reader = ClaudeRecordReader(now_ns=now_ns, claude_home=tmp_path, session_id="missing-session")
    assert asyncio.run(reader.records()) == ()
    assert _read(reader, "main", None) is None


def test_parsing_the_stored_rows_gives_the_entries_the_read_gave(tmp_path: Path) -> None:
    home, _ = _home(tmp_path, "subagents")
    reader = ClaudeRecordReader(now_ns=now_ns, claude_home=home, session_id=_session("subagents"))
    batch = _read(reader, "main", None)
    assert batch is not None and batch.rows
    again = ClaudeRecordReader(now_ns=now_ns, claude_home=home, session_id=_session("subagents"))
    rebuilt = again.parse_stored(batch.rows)
    assert _keys(list(rebuilt)) == _keys(list(batch.entries))
    assert [e.order.seq for e in rebuilt] == [e.order.seq for e in batch.entries]


def _user_row_uuid(scenario: str) -> tuple[int, str]:
    for index, row in enumerate(rows(scenario)):
        value = row.get("uuid")
        if row.get("type") == "user" and isinstance(value, str):
            return index, value
    raise AssertionError("the scenario has no user row")


# meiva:note
# Starting part-way: the position just before the first row that names one of the wire ids (an input this
# process sent), found by bytes alone; with no match the end of the record; a record with nothing in it has none.
def test_a_record_with_history_can_be_started_just_before_the_first_row_of_an_input_we_sent(
    tmp_path: Path,
) -> None:
    home, _ = _home(tmp_path, "basic")
    sid = _session("basic")
    reader = ClaudeRecordReader(now_ns=now_ns, claude_home=home, session_id=sid)
    index, wire = _user_row_uuid("basic")
    before = asyncio.run(reader.position_before("main", [wire]))
    assert before is not None and decode(before).lines == index
    rest = _read(reader, "main", before)
    assert rest is not None and rest.entries
    assert all(entry.order.seq >= index for entry in rest.entries)
    end = asyncio.run(reader.position_before("main", ["not-in-the-record"]))
    assert end is not None and decode(end).lines == len(rows("basic"))
    assert _read(reader, "main", end) is None
    assert asyncio.run(reader.position_before("main", [])) == end


def test_a_start_before_the_first_row_is_no_position_and_a_partial_last_line_is_not_counted(
    tmp_path: Path,
) -> None:
    home = tmp_path
    folder = home / "projects" / "p"
    folder.mkdir(parents=True)
    rows_text = ['{"type":"user","uuid":"a1"}\n', '{"type":"user","uuid":"b2"}\n']
    _ = (folder / "s1.jsonl").write_text("".join(rows_text) + '{"type":"user","uuid":"c3"')
    reader = ClaudeRecordReader(now_ns=now_ns, claude_home=home, session_id="s1")
    assert asyncio.run(reader.position_before("main", ["a1"])) is None
    second = asyncio.run(reader.position_before("main", ["b2"]))
    assert second is not None and decode(second).lines == 1
    end = asyncio.run(reader.position_before("main", ["nope"]))
    assert end is not None and decode(end).lines == 2
    missing = ClaudeRecordReader(now_ns=now_ns, claude_home=tmp_path / "empty", session_id="x")
    assert asyncio.run(missing.position_before("main", [])) is None


def test_the_same_session_under_two_project_directories_reads_the_one_of_the_agents_workspace(
    tmp_path: Path,
) -> None:
    home, target = _home(tmp_path, "basic")
    sid = _session("basic")
    workspace = tmp_path / "work" / "agent_7"
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(workspace))
    own = home / "projects" / slug
    own.mkdir(parents=True)
    lines = target.read_text().splitlines(keepends=True)
    _ = (own / f"{sid}.jsonl").write_text("".join(lines[:5]))
    os.utime(own / f"{sid}.jsonl", (1, 1))
    reader = ClaudeRecordReader(
        now_ns=now_ns, claude_home=home, session_id=sid, workspace=workspace
    )
    batch = _read(reader, "main", None)
    assert batch is not None and decode(batch.position).lines == 5
    elsewhere = ClaudeRecordReader(
        now_ns=now_ns, claude_home=home, session_id=sid, workspace=tmp_path / "other"
    )
    newest = _read(elsewhere, "main", None)
    assert newest is not None and decode(newest.position).lines == len(rows("basic"))
    _ = (own / f"{sid}.jsonl").unlink()
    with pytest.raises(PositionLostError):
        _ = _read(reader, "main", batch.position)


def test_a_session_id_that_is_not_known_yet_reads_nothing_until_it_is(tmp_path: Path) -> None:
    home, _ = _home(tmp_path, "basic")
    sid = _session("basic")
    known: list[str | None] = [None]
    reader = ClaudeRecordReader(now_ns=now_ns, claude_home=home, session_id=lambda: known[0])
    assert asyncio.run(reader.records()) == () and _read(reader, "main", None) is None
    known[0] = sid
    assert asyncio.run(reader.records()) == ("main",)
    assert _read(reader, "main", None) is not None
    assert reader.wire_id("k") == ClaudeRecordReader(
        now_ns=now_ns, claude_home=home, session_id=sid
    ).wire_id("k")


# meiva:note
# Row types a long real session (CLI 2.1.322) carries besides the conversation: titles, links, renames, file-history
# deltas, the informational and stop-hook system rows and the context attachments. None is a conversation fact, so
# none is an anomaly (an anomaly is reported once per type, and these are not the ones to report).
def test_the_non_conversation_rows_of_a_long_session_are_ignored_not_reported() -> None:
    stamp = "2026-11-12T09:00:00Z"
    raws: list[dict[str, object]] = [
        {"type": "ai-title", "aiTitle": "t"},
        {"type": "custom-title", "customTitle": "t"},
        {"type": "agent-name", "agentName": "n"},
        {"type": "pr-link", "prNumber": 1},
        {"type": "bridge-session", "x": 1},
        {"type": "relocated", "x": 1},
        {"type": "file-history-delta", "x": 1},
        {"type": "system", "subtype": "informational", "uuid": "u1", "timestamp": stamp},
        {"type": "system", "subtype": "stop_hook_summary", "uuid": "u2", "timestamp": stamp},
    ]
    for index, kind in enumerate(
        ("deferred_tools_record", "instructions", "file", "task_status", "nested_memory")
    ):
        raws.append(
            {
                "type": "attachment",
                "uuid": f"a{index}",
                "timestamp": stamp,
                "attachment": {"type": kind},
            }
        )
    state = ParseState()
    seen: list[AnyEntry] = []
    for line, raw in enumerate(raws):
        state, parsed = parse_row(state, raw, line=line)
        seen.extend(parsed)
    assert seen == []


# meiva:note
# A finished background command can reach a running turn as a `queued_command` attachment whose own `origin` says
# `task-notification` (recorded live, CLI 2.1.322): the CLI telling the conversation something, never an input of
# ours, so it is a notice that opens no turn and not a human input nobody sent.
def test_a_notification_queued_into_a_running_turn_is_a_notice_and_not_an_unmatched_input() -> None:
    prompt = (
        "<task-notification><task-id>b1</task-id><tool-use-id>toolu_1</tool-use-id>"
        "<status>completed</status><summary>done</summary></task-notification>"
    )
    row: dict[str, object] = {
        "type": "attachment",
        "uuid": "q1",
        "timestamp": "2026-11-12T09:00:00Z",
        "attachment": {
            "type": "queued_command",
            "prompt": prompt,
            "source_uuid": "not-ours",
            "origin": {"kind": "task-notification"},
        },
    }
    _, parsed = parse_row(ParseState(), row, line=0)
    [told] = parsed
    assert isinstance(told, HumanInput) and told.origin is not None and told.joined_running_turn
    assert told.origin.call_id == "toolu_1"
    assert "Background task b1 completed" in told.origin.text
    plain = {**row, "attachment": {"type": "queued_command", "prompt": "hi", "source_uuid": "w"}}
    _, steered = parse_row(ParseState(), plain, line=1)
    assert [type(entry).__name__ for entry in steered] == ["HumanInput"]


def test_polling_an_unchanged_file_opens_and_hashes_nothing_and_a_touched_one_is_read_again(
    tmp_path: Path,
) -> None:
    home, target = _home(tmp_path, "basic")
    reader = ClaudeRecordReader(now_ns=now_ns, claude_home=home, session_id=_session("basic"))
    batch = _read(reader, "main", None)
    assert batch is not None
    assert _read(reader, "main", batch.position) is None, "the first answer checks the last row"
    settled = reader.bytes_read
    for _ in range(5):
        assert _read(reader, "main", batch.position) is None
    assert reader.bytes_read == settled, "five polls of a file that did not change read nothing"
    with target.open("a") as handle:
        _ = handle.write(rows_text("api_error"))
    assert _read(reader, "main", batch.position) is not None, "a longer file is read"


def test_a_batch_reports_the_bytes_it_covers_for_the_writers_transaction_budget(
    tmp_path: Path,
) -> None:
    home, target = _home(tmp_path, "basic")
    reader = ClaudeRecordReader(now_ns=now_ns, claude_home=home, session_id=_session("basic"))
    batch = _read(reader, "main", None)
    assert batch is not None and batch.size == target.stat().st_size
    assert reader.cli_version == "2.1.322", "the version the rows say, for the stored position"


def test_the_sub_agent_listing_is_remembered_until_the_folder_changes(tmp_path: Path) -> None:
    home, _ = _home(tmp_path, "subagents", copy_subagents=True)
    reader = ClaudeRecordReader(now_ns=now_ns, claude_home=home, session_id=_session("subagents"))
    first = asyncio.run(reader.records())
    assert first[0] == "main" and len(first) == 3
    folder = home / "projects" / "project" / _session("subagents") / "subagents"
    _ = shutil.copyfile(next(folder.glob("agent-*.jsonl")), folder / "agent-late.jsonl")
    assert "late" in asyncio.run(reader.records()), "a file added to the folder is listed"
