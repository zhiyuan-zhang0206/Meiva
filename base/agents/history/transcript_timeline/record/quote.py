from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from base.agents.history.transcript_timeline.identity import (
    record_identity,
    record_quote_coordinates,
)
from base.agents.history.transcript_timeline.record.harness import entry_reader
from base.lm.harness.session.transcript.entries import (
    MAIN_RECORD,
    AgentText,
    AnyEntry,
    HumanInput,
    Reasoning,
    ToolCall,
    ToolResult,
)
from base.lm.harness.session.transcript.projection import Projector
from base.lm.harness.session.transcript.record_file import QuoteRow, RecordFile, UnreadRows


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
    inbound_id = query.inbound_id
    with file.quote_row(offset, inbound=inbound_id is not None) as result:
        if result is None:
            return None
        reader = entry_reader(query.harness, unread)
        entries = reader.entries([result.row])
        if inbound_id is not None:
            return _inbound_match(query, inbound_id, offset, entries, inbound_matches)
        return _record_match(query, result, entries, pending_calls=reader.pending_calls)


def _record_match(
    query: RecordQuoteQuery,
    result: QuoteRow,
    entries: tuple[AnyEntry, ...],
    *,
    pending_calls: bool,
) -> RecordQuoteMatch | None:
    for entry in entries:
        if _quoteable(entry) and _matches(query, result.row.offset, entry.key):
            return RecordQuoteMatch(
                created_at=entry.at,
                activity_pending=(
                    isinstance(entry, ToolCall)
                    and pending_calls
                    and _is_unanswered(entry, entries)
                    and result.is_tail()
                ),
            )
    return None


def _inbound_match(
    query: RecordQuoteQuery,
    inbound_id: int,
    offset: int,
    entries: tuple[AnyEntry, ...],
    inbound_matches: Callable[[int, frozenset[str]], bool],
) -> RecordQuoteMatch | None:
    if not _matches(query, offset, inbound_id):
        return None
    for entry in entries:
        if (
            isinstance(entry, HumanInput)
            and entry.record == MAIN_RECORD
            and entry.wire_id is not None
            and inbound_matches(inbound_id, frozenset({entry.wire_id}))
        ):
            return RecordQuoteMatch(created_at=entry.at)
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
