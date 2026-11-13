from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.lm.harness.claude.delivery import key_uuid
from base.lm.harness.claude.reconciliation.transcript import claude_home
from base.lm.harness.claude.transcript.parse import ParseState, parse_row
from base.lm.harness.session.transcript.entries import MAIN_RECORD, Anomaly, AnyEntry
from base.lm.harness.session.transcript.record_file import FileRow, UnreadRows, stamp_of

# meiva:note
# The Claude half of reading a session's timeline at the moment it is asked for: where the main record
# lives, which bytes of it to read and what its rows mean, and nothing about what a timeline shows. A
# row's identity is its byte offset in the file (the file only grows, so an offset is stable, ordered and
# the same however the row was reached), and the offset is also the `seq` of the entries the parser reads
# from it. Reads go backwards from a line boundary, one block at a time, so what a request reads follows
# the size of the window it asks for and not the size of the file. A row that cannot be decoded is
# skipped: the file is the CLI's own and its rows are never repaired here. Sub-agent files are not read.
# meiva:link base/lm/harness/docs/transcript-projection.md


def wire_id(key: str) -> str:
    return key_uuid(key)


# meiva:note
# Where the session's file is looked for, for a message that says so: the session's project directory under its own
# Claude home (every other project directory is searched after it).
def record_location(environ: Mapping[str, str], workspace: Path) -> str:
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(workspace))
    return str(claude_home(None, environ) / "projects" / slug)


# meiva:note
# The record file of a session as the session itself finds it: the Claude home is the one the session's own
# environment names (`$HOME/.claude`, or the configured directory), the working directory is the session's.
def session_file(environ: Mapping[str, str], workspace: Path, session_id: str) -> Path | None:
    return transcript_path(claude_home(None, environ), workspace, session_id)


# meiva:note
# The project directory name the CLI derives from the working directory, and the file the reader of the
# live path uses first; the session's id is unique, so a search of every project directory is the fallback
# when the CLI filed it under another name.
def transcript_path(claude_home: Path, workspace: Path, session_id: str) -> Path | None:
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(workspace))
    own = claude_home / "projects" / slug / f"{session_id}.jsonl"
    if own.is_file():
        return own
    matches = sorted((claude_home / "projects").glob(f"*/{session_id}.jsonl"))
    if not matches:
        return None
    own_dir = [path for path in matches if path.parent.name == slug]
    if own_dir:
        return own_dir[0]
    return max(matches, key=lambda path: (path.stat().st_mtime_ns, str(path)))


# meiva:note
# Rows read into entries, in file order, each entry named by its row's offset. The parser's only memory is when each
# open tool call started, for the call's duration, so a parser that is given the rows of one read after another
# (a live read) and one given them all at once (a cold read) agree. A row of another record (a sub-agent's row
# that the main file should not hold) yields nothing. A row the parser cannot use is skipped and reported once for
# each kind of row by this parser.
class EntryParser:
    def __init__(self) -> None:
        self._state = ParseState()
        self._unread = UnreadRows("Claude Code")

    def entries(self, rows: Sequence[FileRow]) -> tuple[AnyEntry, ...]:
        found: list[AnyEntry] = []
        for file_row in rows:
            self._state, parsed = parse_row(self._state, file_row.row, line=file_row.offset)
            for entry in parsed:
                if isinstance(entry, Anomaly):
                    self._unread.report(entry)
                elif entry.record == MAIN_RECORD:
                    found.append(entry)
        return tuple(found)


# meiva:note
# The ids the CLI kept for the person's inputs among the rows, each with the time its row was written: the ids to match
# to inbox rows. A user row names its own `uuid`, an absorbed message the `source_uuid` of its attachment.
def input_wire_ids(rows: Sequence[FileRow]) -> dict[str, datetime]:
    wanted: dict[str, datetime] = {}
    for row in rows:
        stamp = stamp_of(row.row.get("timestamp"))
        wire = _wire_of(row.row)
        if stamp is not None and wire is not None and is_input_row(row.row):
            wanted[wire] = stamp
    return wanted


def _wire_of(row: Mapping[str, object]) -> str | None:
    attachment = row.get("attachment")
    wire = attachment.get("source_uuid") if is_object_dict(attachment) else row.get("uuid")
    return wire if isinstance(wire, str) else None


# meiva:note
# A row that can be a person's input (a user row that is not a tool result, or an absorbed message): the only rows the
# inbox is asked about, so a stretch of the record with none costs no query.
def is_input_row(row: Mapping[str, object]) -> bool:
    if row.get("type") == "attachment":
        attachment = row.get("attachment")
        return is_object_dict(attachment) and attachment.get("type") == "queued_command"
    if row.get("type") != "user":
        return False
    origin = row.get("origin")
    if is_object_dict(origin) and origin.get("kind") not in (None, "human"):
        return False
    message = row.get("message")
    content = message.get("content") if is_object_dict(message) else None
    return not (
        is_object_list(content)
        and any(is_object_dict(block) and block.get("type") == "tool_result" for block in content)
    )
