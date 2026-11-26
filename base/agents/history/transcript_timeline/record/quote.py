from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from base.agents.history.transcript_timeline.identity import (
    record_identity,
    record_quote_coordinates,
)
from base.agents.history.transcript_timeline.record.harness import entry_reader, input_wire_ids
from base.lm.harness.session.transcript.entries import (
    AgentText,
    AnyEntry,
    Reasoning,
    ToolCall,
    ToolResult,
)
from base.lm.harness.session.transcript.projection import Projector
from base.lm.harness.session.transcript.record_file import FileRow, RecordFile, UnreadRows


@dataclass(frozen=True, slots=True)
class RecordQuoteQuery:
    agent_id: int
    harness: str
    session: str
    identity: str
    inbound_id: int | None


@dataclass(frozen=True, slots=True)
class RecordQuoteMatch:
    created_at: str | None
    activity_pending: bool = False


def read_record_quote(
    file: RecordFile,
    query: RecordQuoteQuery,
    *,
    inbound_matches: Callable[[int, frozenset[str]], bool],
    unread: UnreadRows,
) -> RecordQuoteMatch | None:
    coordinates = record_quote_coordinates(query.identity)
    if coordinates is None or coordinates.session != query.session:
        return None
    offset = coordinates.offset
    file.prepare_reading(offset)
    row = file.row_at(offset)
    if row is None:
        return None
    if query.inbound_id is not None:
        return _inbound_match(query, query.inbound_id, row, inbound_matches)
    reader = entry_reader(query.harness, unread)
    entries = reader.entries([row])
    for entry in entries:
        if _quoteable(entry) and _matches(query, offset, entry.key):
            return RecordQuoteMatch(
                created_at=entry.at,
                activity_pending=(
                    isinstance(entry, ToolCall)
                    and reader.pending_calls
                    and row.end == file.complete_record_end()
                    and _is_unanswered(entry, entries)
                ),
            )
    return None


def _inbound_match(
    query: RecordQuoteQuery,
    inbound_id: int,
    row: FileRow,
    inbound_matches: Callable[[int, frozenset[str]], bool],
) -> RecordQuoteMatch | None:
    wanted = input_wire_ids(query.harness, [row])
    if (
        wanted
        and _matches(query, row.offset, inbound_id)
        and inbound_matches(inbound_id, frozenset(wanted))
    ):
        return RecordQuoteMatch(
            created_at=min(found.stamp for found in wanted.values()).isoformat()
        )
    return None


def _matches(query: RecordQuoteQuery, offset: int, key: str | int) -> bool:
    return query.identity == record_identity(
        query.agent_id, query.harness, query.session, offset, key
    )


def _quoteable(entry: AnyEntry) -> bool:
    match entry:
        case AgentText(text=text, complete=True) | Reasoning(text=text):
            return bool(text.strip())
        case ToolCall() | ToolResult():
            return True
        case _:
            return False


def _is_unanswered(call: ToolCall, entries: tuple[AnyEntry, ...]) -> bool:
    projector = Projector()
    _ = projector.feed(entries, release_held=True)
    return call.call_id in projector.unanswered_calls()
