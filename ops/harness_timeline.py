from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

import zstandard
from psycopg_pool import ConnectionPool

from base.agents.history.timeline_item import TimelineItem
from base.agents.history.transcript_timeline.items import ReplyPolicy, format_tool_use
from base.agents.history.transcript_timeline.record.harness import (
    compressed_file,
    session_file,
    unreadable_file,
    wire_id,
)
from base.agents.history.transcript_timeline.record.read import read_window
from base.agents.history.transcript_timeline.sent import load_sent_rows, sent_inputs
from base.api_contracts.agent_contract import reads_timeline_from_record
from base.lm.harness.registry import FileSessionRegistry
from base.lm.harness.session.transcript.entries import InputRow
from base.lm.harness.session.transcript.record_file import RecordFile, ZstdRecordFile
from base.log import logger
from base.paths import harness_sessions_dir
from ops.rpc_schemas import HarnessTimelineReadRequest, HarnessTimelineWindow


# meiva:note
# The `harness_timeline_read` operation: the timeline window of an agent whose CLI keeps its own record, read on the
# machine the agent lives on, from the record file at the moment it is asked for. The session id and the working
# directory come from the agent's session record (`<home>/harness/sessions`, the machine's own file), the file from the
# session's own home for the CLI's records (the Claude home, the Codex home), and what the window shows from the file and the inbox rows of the same time. An agent
# with no session record yet, or whose file is not written yet, has an empty timeline, not an error. A harness this
# operation does not read is refused.
# meiva:link docs/decisions/agents/graph/2026-11-13-timeline-read-from-the-record.md
def read_harness_timeline(
    request: HarnessTimelineReadRequest,
    *,
    pool: ConnectionPool,
    home: Path,
    environ: Mapping[str, str],
) -> HarnessTimelineWindow:
    if not reads_timeline_from_record(request.harness):
        raise ValueError(f"the timeline of harness {request.harness!r} is not read from its record")
    record = FileSessionRegistry(harness_sessions_dir(home=home)).load(
        request.agent_id, harness=request.harness
    )
    if record is None:
        return _EMPTY
    path = session_file(request.harness, environ, Path(record.cwd), record.session_id)
    file: RecordFile
    if path is not None:
        file = RecordFile(path)
    else:
        packed = compressed_file(request.harness, environ, record.session_id)
        if packed is None:
            _report_unreadable(request, environ, record.session_id)
            return _EMPTY
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
        window = read_window(
            file,
            harness=request.harness,
            limit=request.limit,
            before=request.before,
            policy=ReplyPolicy(request.explicit_visibility, request.delivery_mode),
            render_tool=format_tool_use,
            load_sent=load_sent,
        )
    except FileNotFoundError:
        return _EMPTY
    except zstandard.ZstdError as error:
        _report_unreadable(request, environ, record.session_id, reason=str(error))
        return _EMPTY
    return HarnessTimelineWindow(
        items=window.timeline_items, has_more=window.has_more, size=window.size
    )


_EMPTY = HarnessTimelineWindow(items=[], has_more=False, size=0)


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
