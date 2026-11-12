from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol

from base.api_contracts.agent_contract import Harness
from base.lm.harness.claude.transcript import window as claude
from base.lm.harness.codex.transcript import window as codex
from base.lm.harness.session.transcript.entries import AnyEntry
from base.lm.harness.session.transcript.record_file import FileRow

# meiva:note
# What differs between the CLIs whose timeline is read from their own record, chosen by the harness: where the record
# file is, how the id we sent an input with is spelled in it, which rows are inputs, how a run of rows is read into
# entries, and the key a reply is delivered under. Everything after that (the window read, the projection, the inbound
# mapping, the live sink, the operation, the gateway) is one piece of code for all of them. A harness whose timeline is
# not read from its record is refused here, where the answer is decided once (`reads_timeline_from_record`).
# meiva:link base/lm/harness/docs/transcript-projection.md


class EntryReader(Protocol):
    def entries(self, rows: Sequence[FileRow]) -> tuple[AnyEntry, ...]: ...


def entry_reader(harness: str) -> EntryReader:
    match Harness(harness):
        case Harness.CLAUDE_CODE:
            return claude.EntryParser()
        case Harness.CODEX:
            return codex.EntryParser()
        case other:
            raise ValueError(f"the timeline of harness {other.value!r} is not read from its record")


def wire_id(harness: str, key: str) -> str:
    match Harness(harness):
        case Harness.CLAUDE_CODE:
            return claude.wire_id(key)
        case Harness.CODEX:
            return codex.wire_id(key)
        case other:
            raise ValueError(f"the timeline of harness {other.value!r} is not read from its record")


def input_wire_ids(harness: str, rows: Sequence[FileRow]) -> dict[str, datetime]:
    match Harness(harness):
        case Harness.CLAUDE_CODE:
            return claude.input_wire_ids(rows)
        case Harness.CODEX:
            return codex.input_wire_ids(rows)
        case other:
            raise ValueError(f"the timeline of harness {other.value!r} is not read from its record")


# meiva:note
# The record file of a session as the session itself finds it, or None when it is not there (a thread that has run no
# turn has no file yet). `workspace` is the session's working directory, which Claude files its record under and Codex
# does not need.
def session_file(
    harness: str, environ: Mapping[str, str], workspace: Path, session_id: str
) -> Path | None:
    match Harness(harness):
        case Harness.CLAUDE_CODE:
            return claude.session_file(environ, workspace, session_id)
        case Harness.CODEX:
            return codex.session_file(environ, session_id)
        case other:
            raise ValueError(f"the timeline of harness {other.value!r} is not read from its record")


# meiva:note
# Where the file was looked for, for a message.
def record_location(harness: str, environ: Mapping[str, str], workspace: Path) -> str:
    match Harness(harness):
        case Harness.CLAUDE_CODE:
            return claude.record_location(environ, workspace)
        case Harness.CODEX:
            return codex.record_location(environ)
        case other:
            raise ValueError(f"the timeline of harness {other.value!r} is not read from its record")


# meiva:note
# The name of a file of the session that was found but is not of a form this reader reads (None when there is none:
# Claude's record has only the one form). A file that is there and unreadable is told from one that is not there.
def unreadable_file(harness: str, environ: Mapping[str, str], session_id: str) -> str | None:
    match Harness(harness):
        case Harness.CODEX:
            return codex.unreadable_file(environ, session_id)
        case _:
            return None


# meiva:note
# A reply's publication key: the CLI's own id of the row that holds it, under a prefix per harness, so it is the same
# however often and from wherever the row is read and cannot meet a key of another scheme. `entry_key` is the key of the
# entry the reply was projected from.
REPLY_KEY_PREFIX = {Harness.CLAUDE_CODE: "harness:claude:", Harness.CODEX: "harness:codex:"}


def reply_key_prefix(harness: str) -> str:
    return REPLY_KEY_PREFIX[Harness(harness)]


def reply_key(harness: str, entry_key: str) -> str:
    match Harness(harness):
        case Harness.CLAUDE_CODE:
            return f"{reply_key_prefix(harness)}{entry_key.removeprefix('claude:')}"
        case Harness.CODEX:
            return f"{reply_key_prefix(harness)}{entry_key.removeprefix('agent:')}"
        case other:
            raise ValueError(f"the timeline of harness {other.value!r} is not read from its record")
