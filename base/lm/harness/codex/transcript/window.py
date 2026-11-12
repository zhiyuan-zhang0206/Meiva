from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

from base.host.boundary.json_values import is_object_dict
from base.lm.harness.codex.reconciliation.rollout import codex_home, rollout_path
from base.lm.harness.codex.transcript.rollout import parse_rollout_line
from base.lm.harness.session.transcript.entries import MAIN_RECORD, Anomaly, AnyEntry, HumanInput
from base.lm.harness.session.transcript.record_file import FileRow, stamp_of
from base.log import logger

# meiva:note
# The Codex half of reading a thread's timeline at the moment it is asked for: where the rollout file lives, which
# rows of it hold a person's input and what its rows mean; the reading of bytes is shared (`session/transcript/
# record_file.py`) and what a timeline shows is the shared projection. The file is the one the thread itself writes
# (`<codex home>/sessions/YYYY/MM/DD/rollout-<time>-<thread id>.jsonl`, the home being the one the session's own
# environment names), read from disk and not through the app-server connection: the cold read runs in another
# process than the agent host and has no connection, and a file has no lifecycle that can be mistaken for "nothing
# there". A rollout only grows (a resume appends to the same file, a compaction appends, a fork starts a new file with
# only its own rows), so a row's byte offset is its identity, and it replaces the rollout's own `ordinal` as the `seq`
# of the entries read from it. A thread that has run no turn has no file yet. The input key we send is the
# `clientUserMessageId`, so the wire id of an input is its key.
# meiva:link base/lm/harness/codex/docs/codex.md


def wire_id(key: str) -> str:
    return key


def record_location(environ: Mapping[str, str]) -> str:
    return str(codex_home(environ) / "sessions")


def session_file(environ: Mapping[str, str], thread_id: str) -> Path | None:
    return rollout_path(codex_home(environ), thread_id)


# meiva:note
# A file the thread has under its home that this reader does not read (Codex may keep a rollout in a compressed
# form; none was met): its name, so that "found but unreadable" is told from "not there".
def unreadable_file(environ: Mapping[str, str], thread_id: str) -> str | None:
    found = sorted(
        path.name
        for path in (codex_home(environ) / "sessions").glob(f"*/*/*/rollout-*{thread_id}*")
        if not path.name.endswith(".jsonl")
    )
    return found[-1] if found else None


# meiva:note
# Rows read into entries, in file order, each entry named by its row's offset. The parser's only memory is the turns
# whose first input it has read (a second input of a turn joined the running turn), so a parser given the rows of one
# read after another and one given them all at once agree. A row the parser cannot use is skipped, and reported once
# per kind of row for this parser (not an error: the file is the CLI's own and moves with its version).
class EntryParser:
    def __init__(self) -> None:
        self._opened: set[str] = set()
        self._reported: set[tuple[str, str]] = set()

    def entries(self, rows: Sequence[FileRow]) -> tuple[AnyEntry, ...]:
        found: list[AnyEntry] = []
        for file_row in rows:
            read = parse_rollout_line(
                {**file_row.row, "ordinal": file_row.offset},
                record=MAIN_RECORD,
                input_turns=self._opened,
            )
            for entry in read:
                if isinstance(entry, Anomaly):
                    self._report(entry)
                    continue
                if isinstance(entry, HumanInput) and entry.turn is not None:
                    self._opened.add(entry.turn)
                found.append(entry)
        return tuple(found)

    def _report(self, anomaly: Anomaly) -> None:
        mark = (anomaly.anomaly.value, anomaly.detail)
        if mark in self._reported:
            return
        self._reported.add(mark)
        logger.warning(
            "a row of the Codex rollout is not read: {kind} {detail}",
            event="harness_record_row_unread",
            kind=anomaly.anomaly.value,
            detail=anomaly.detail,
        )


# meiva:note
# The ids Codex kept for the person's inputs among the rows, each with the time its row was written: the ids to match
# to inbox rows. Only a user message that carries our `clientUserMessageId` can be one of ours.
def input_wire_ids(rows: Sequence[FileRow]) -> dict[str, datetime]:
    wanted: dict[str, datetime] = {}
    for row in rows:
        wire = _client_id(row.row)
        stamp = stamp_of(row.row.get("timestamp"))
        if wire is not None and stamp is not None:
            wanted[wire] = stamp
    return wanted


def _client_id(row: Mapping[str, object]) -> str | None:
    payload = row.get("payload")
    if row.get("type") != "event_msg" or not is_object_dict(payload):
        return None
    item = payload.get("item")
    if payload.get("type") != "item_completed" or not is_object_dict(item):
        return None
    wire = item.get("client_id")
    return wire if item.get("type") == "UserMessage" and isinstance(wire, str) else None
