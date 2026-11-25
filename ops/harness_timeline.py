from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

import zstandard
from psycopg_pool import ConnectionPool

from base.agents.history.timeline import reading_target, reading_window
from base.agents.history.timeline_item import TimelineItem
from base.agents.history.transcript_timeline.items import ReplyPolicy, format_tool_use
from base.agents.history.transcript_timeline.project import ProjectedItem
from base.agents.history.transcript_timeline.record.harness import (
    compressed_file,
    record_file,
    reply_key,
    session_file,
    unreadable_file,
    wire_id,
)
from base.agents.history.transcript_timeline.record.read import (
    LoadSent,
    read_reading_window,
    read_window,
)
from base.agents.history.transcript_timeline.sent import load_sent_rows, sent_inputs
from base.api_contracts.agent_contract import reads_timeline_from_record
from base.api_contracts.reading import (
    ReadingSourceUnavailableError,
    ReadingUnavailableReason,
    RecordReadingLocator,
    RecordReadingSource,
    decode_reading_locator,
    encode_reading_locator,
    encode_reading_source,
)
from base.lm.harness.registry import FileSessionRegistry, SessionRecord
from base.lm.harness.session.transcript.entries import InputRow
from base.lm.harness.session.transcript.record_file import RecordFile, UnreadRows, ZstdRecordFile
from base.log import logger
from base.paths import harness_sessions_dir
from ops.rpc_schemas import HarnessTimelineReadRequest, HarnessTimelineWindow, RecordUnreadable


# meiva:note
# The `harness_timeline_read` operation: the timeline window of an agent whose CLI keeps its own record, read on the
# machine the agent lives on, from the record file at the moment it is asked for. The session id and the working
# directory come from the agent's session record (`<home>/harness/sessions`, the machine's own file), the file from the
# session's own home for the CLI's records (the Claude home, the Codex home), and what the window shows from the file and the inbox rows of the same time. An agent
# with no session record yet, or whose file is not written yet, has an empty timeline, not an error. A harness this
# operation does not read is refused. `unread` is the daemon's own: a row of a kind the parser does not know is said once
# by the process, not once by every poll of the page that shows it.
# meiva:link docs/decisions/agents/graph/2026-11-13-timeline-read-from-the-record.md
def read_harness_timeline(
    request: HarnessTimelineReadRequest,
    *,
    pool: ConnectionPool,
    home: Path,
    environ: Mapping[str, str],
    unread: UnreadRows,
) -> HarnessTimelineWindow:
    if not reads_timeline_from_record(request.harness):
        raise ValueError(f"the timeline of harness {request.harness!r} is not read from its record")
    record = _session_of(request, home)
    if isinstance(record, RecordUnreadable):
        return _empty(request, record)
    path = session_file(request.harness, environ, Path(record.cwd), record.session_id)
    file: RecordFile
    if path is not None:
        file = record_file(request.harness, path)
    else:
        packed = compressed_file(request.harness, environ, record.session_id)
        if packed is None:
            _report_unreadable(request, environ, record.session_id)
            return _empty(request, RecordUnreadable.FILE_MISSING)
        file = ZstdRecordFile(packed)

    def load_sent(until: datetime, wanted: Mapping[str, InputRow]) -> dict[str, list[TimelineItem]]:
        with pool.connection() as conn:
            rows = load_sent_rows(
                conn,
                request.agent_id,
                until=until,
                wanted=wanted,
                wire_id=lambda key: wire_id(request.harness, key),
            )
        return sent_inputs(rows.rows)

    try:
        if request.reading_locator is not None:
            return _reading_window(request, file, record.session_id, load_sent, unread)
        window = read_window(
            file,
            harness=request.harness,
            limit=request.limit,
            before=request.before,
            anchor=int(request.anchor_item_id.split(".")[0][1:])
            if request.anchor_item_id
            else None,
            policy=ReplyPolicy(request.explicit_visibility, request.delivery_mode),
            render_tool=format_tool_use,
            load_sent=load_sent,
            unread=unread,
            floor=request.floor or 0,
        )
    except FileNotFoundError:
        return _empty(request, RecordUnreadable.FILE_MISSING)
    except zstandard.ZstdError as error:
        if request.reading_locator is not None:
            raise
        _report_unreadable(request, environ, record.session_id, reason=str(error))
        return _empty(request, RecordUnreadable.UNDECODABLE)
    items = _with_record_locators(window.timeline_items, request, record.session_id, window.items)
    verified = (
        request.anchor_item_id
        if any(item.item_id == request.anchor_item_id for item in items)
        else None
    )
    return HarnessTimelineWindow(
        items=items,
        has_more=window.has_more,
        size=window.size,
        anchor_item_id=verified,
        anchor_applied=request.anchor_item_id is not None,
    )


def _session_of(
    request: HarnessTimelineReadRequest, home: Path
) -> SessionRecord | RecordUnreadable:
    record = FileSessionRegistry(harness_sessions_dir(home=home)).load(
        request.agent_id, harness=request.harness
    )
    if record is None:
        return RecordUnreadable.NO_SESSION
    if request.session_id is not None and request.session_id != record.session_id:
        return RecordUnreadable.SESSION_MISMATCH
    return record


def _empty(
    request: HarnessTimelineReadRequest, unreadable: RecordUnreadable | None = None
) -> HarnessTimelineWindow:
    return HarnessTimelineWindow(
        items=[],
        has_more=False,
        size=0,
        unreadable=unreadable,
        anchor_applied=request.anchor_item_id is not None,
        reading_applied=request.reading_locator is not None,
        reading_unavailable=ReadingUnavailableReason.SOURCE
        if request.reading_locator is not None
        else None,
    )


def _record_source(request: HarnessTimelineReadRequest, session: str) -> RecordReadingSource:
    return RecordReadingSource.model_validate(
        {
            "agent_id": request.agent_id,
            "harness": request.harness,
            "session_id": session,
        }
    )


def _with_record_locators(
    items: list[TimelineItem],
    request: HarnessTimelineReadRequest,
    session: str,
    projected: Sequence[ProjectedItem],
) -> list[TimelineItem]:
    source = _record_source(request, session)
    entry_keys = {shown.item.item_id: shown.key for shown in projected}
    bound: list[TimelineItem] = []
    for item in items:
        offset, block = item.item_id.split(".")
        locator = RecordReadingLocator(
            agent_id=source.agent_id,
            harness=source.harness,
            session_id=source.session_id,
            offset=int(offset[1:]),
            block=int(block),
        )
        update: dict[str, object] = {"reading_locator": encode_reading_locator(locator)}
        if item.kind == "agent_chat":
            update["publication_key"] = reply_key(
                request.harness, entry_keys[item.item_id], session
            )
        bound.append(item.model_copy(update=update))
    return bound


def _reading_window(
    request: HarnessTimelineReadRequest,
    file: RecordFile,
    session: str,
    load_sent: LoadSent,
    unread: UnreadRows,
) -> HarnessTimelineWindow:
    if request.reading_locator is None or request.reading_direction is None:
        raise ValueError("A source reading selector is required")
    locator = decode_reading_locator(request.reading_locator)
    source = _record_source(request, session)
    if not isinstance(locator, RecordReadingLocator) or (
        locator.agent_id,
        locator.harness,
        locator.session_id,
    ) != (request.agent_id, request.harness, session):
        return _reading_unavailable(file, source, ReadingUnavailableReason.SOURCE)
    try:
        window = read_reading_window(
            file,
            harness=request.harness,
            offset=locator.offset,
            policy=ReplyPolicy(request.explicit_visibility, request.delivery_mode),
            render_tool=format_tool_use,
            load_sent=load_sent,
            unread=unread,
        )
        items = _with_record_locators(window.timeline_items, request, session, window.items)
        target = reading_target(items, locator)
        selected = reading_window(
            items,
            target,
            request.reading_direction,
            request.limit,
            older_available=window.has_more,
            newer_available=window.has_newer,
        )
        contextual = window.with_call_context(selected.items)
        selected_items = _with_record_locators(contextual, request, session, window.items)
        return HarnessTimelineWindow(
            items=selected_items,
            has_more=selected.older_cursor is not None,
            size=window.size,
            anchor_item_id=selected.anchor_item_id,
            reading_applied=True,
            reading_source=encode_reading_source(source),
            older_cursor=selected.older_cursor,
            newer_cursor=selected.newer_cursor,
        )
    except ReadingSourceUnavailableError as exc:
        reason = (
            ReadingUnavailableReason.BUDGET
            if exc.reason is ReadingUnavailableReason.SOURCE
            else exc.reason
        )
        return _reading_unavailable(file, source, reason)


def _reading_unavailable(
    file: RecordFile, source: RecordReadingSource, reason: ReadingUnavailableReason
) -> HarnessTimelineWindow:
    return HarnessTimelineWindow(
        items=[],
        has_more=False,
        size=file.size(),
        reading_applied=True,
        reading_source=encode_reading_source(source),
        reading_unavailable=reason,
    )


# meiva:note
# A session file that is there but not of a form this operation reads (or a compressed one that cannot be decoded) is not an
# empty timeline: the page shows nothing, and the event says why, with the thread and the file's name.
def _report_unreadable(
    request: HarnessTimelineReadRequest,
    environ: Mapping[str, str],
    session_id: str,
    *,
    reason: str | None = None,
) -> None:
    name = unreadable_file(request.harness, environ, session_id, packed_is_readable=reason is None)
    if name is None and reason is not None:
        name = (compressed_file(request.harness, environ, session_id) or Path("?")).name
    if name is not None:
        logger.warning(
            "the record of session {session_id} is in a form this reader does not read: {name}",
            event="harness_record_unreadable",
            agent_id=request.agent_id,
            session_id=session_id,
            name=name,
            reason=reason,
        )
