# meiva:note
# How the reader finds the files of a session without missing one: the listing of the sub-agent folder is not trusted
# when it was made in the same clock tick as the folder's last change (a second file created in that tick leaves the
# modification time where it was), the all-projects search for the session's file is counted per session and made at
# once when a read must not miss the file, and a record that was rewritten is started above the ordinals already stored.
# meiva:link base/lm/harness/docs/transcript-projection.md

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from base.lm.harness.claude.transcript.position import decode
from base.lm.harness.claude.transcript.reader import ClaudeRecordReader
from base.lm.harness.claude.transcript.tests.support import FIXTURES, now_ns, rows

SECOND = 1_000_000_000


def _session() -> str:
    session = rows("basic")[0]["sessionId"]
    assert isinstance(session, str)
    return session


def _main_file(home: Path, project: str, session: str) -> Path:
    folder = home / "projects" / project
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{session}.jsonl"
    target.write_text((FIXTURES / "basic" / "main.jsonl").read_text())
    return target


def test_a_file_created_in_the_same_tick_as_the_last_listing_is_not_missed(tmp_path: Path) -> None:
    session = _session()
    target = _main_file(tmp_path, "p", session)
    folder = target.parent / session / "subagents"
    folder.mkdir(parents=True)
    _ = (folder / "agent-a.jsonl").write_text("")
    stamp = folder.stat().st_mtime_ns
    clock = [stamp + SECOND // 2]
    reader = ClaudeRecordReader(claude_home=tmp_path, session_id=session, now_ns=lambda: clock[0])
    assert asyncio.run(reader.records()) == ("main", "a")

    _ = (folder / "agent-b.jsonl").write_text("")
    os.utime(folder, ns=(stamp, stamp))
    assert asyncio.run(reader.records()) == ("main", "a", "b"), (
        "a listing made in the tick is made again"
    )

    clock[0] = stamp + 5 * SECOND
    assert asyncio.run(reader.records()) == ("main", "a", "b")
    _ = (folder / "agent-c.jsonl").write_text("")
    os.utime(folder, ns=(stamp, stamp))
    assert asyncio.run(reader.records()) == ("main", "a", "b"), (
        "a listing made after the folder was quiet is trusted until the folder changes"
    )
    os.utime(folder, ns=(stamp + SECOND, stamp + SECOND))
    assert asyncio.run(reader.records()) == ("main", "a", "b", "c")


def test_the_search_for_a_session_file_is_counted_per_session_and_exhaustive_when_asked(
    tmp_path: Path,
) -> None:
    first, second = _session(), "99999999-aaaa-bbbb-cccc-000000000000"
    current: list[str] = [first]
    reader = ClaudeRecordReader(
        now_ns=now_ns,
        claude_home=tmp_path,
        session_id=lambda: current[0],
        workspace=tmp_path / "work",
    )
    (tmp_path / "projects").mkdir()
    assert asyncio.run(reader.records()) == ()
    assert asyncio.run(reader.records()) == ()

    current[0] = second
    _ = _main_file(tmp_path, "elsewhere", second)
    assert asyncio.run(reader.records()) == ("main",), (
        "the first poll of a session always searches, whatever was missed for the one before"
    )

    third = "99999999-aaaa-bbbb-cccc-000000000001"
    current[0] = third
    assert asyncio.run(reader.records()) == ()
    assert asyncio.run(reader.records()) == ()
    _ = _main_file(tmp_path, "elsewhere", third)
    assert asyncio.run(reader.records()) == (), "a poll between the searches does not search"
    assert asyncio.run(reader.records(exhaustive=True)) == ("main",), (
        "the last read before a close searches at once"
    )


def test_a_record_started_after_a_rewrite_counts_its_rows_above_the_stored_ordinals(
    tmp_path: Path,
) -> None:
    session = _session()
    _ = _main_file(tmp_path, "p", session)
    reader = ClaudeRecordReader(now_ns=now_ns, claude_home=tmp_path, session_id=session)
    plain = asyncio.run(reader.position_before("main", ()))
    raised = asyncio.run(reader.position_before("main", (), after_ordinal=500))
    assert plain is not None and raised is not None
    assert decode(plain).lines == len(rows("basic"))
    assert decode(raised).lines == 501, (
        "above the highest ordinal stored, which the rewritten file never reached"
    )
    assert decode(raised).offset == decode(plain).offset, "the same place in the file"

    target = tmp_path / "projects" / "p" / f"{session}.jsonl"
    with target.open("a") as handle:
        handle.write(rows_line() + "\n")
    batch = asyncio.run(reader.read_after("main", raised))
    assert batch is not None and all(row.ordinal >= 501 for row in batch.rows)


def rows_line() -> str:
    return (FIXTURES / "basic" / "main.jsonl").read_text().splitlines()[2]
