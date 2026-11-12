from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from psycopg_pool import ConnectionPool

from base.agents.history.timeline_item import TimelineItem
from base.agents.history.transcript_timeline.items import ReplyPolicy, format_tool_use
from base.agents.history.transcript_timeline.read import read_window
from base.agents.history.transcript_timeline.sent import load_sent_rows, sent_inputs
from base.api_contracts.agent_contract import Harness
from base.lm.harness.claude.transcript.window import RecordFile, session_file
from base.lm.harness.registry import FileSessionRegistry
from base.paths import harness_sessions_dir
from ops.rpc_schemas import HarnessTimelineReadRequest, HarnessTimelineWindow


# meiva:note
# The `harness_timeline_read` operation: the timeline window of an agent whose CLI keeps its own record, read on the
# machine the agent lives on, from the record file at the moment it is asked for. The session id and the working
# directory come from the agent's session record (`<home>/harness/sessions`, the machine's own file), the file from the
# session's own Claude home, and what the window shows from the file and the inbox rows of the same time. An agent
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
    if request.harness != Harness.CLAUDE_CODE.value:
        raise ValueError(f"the timeline of harness {request.harness!r} is not read from its record")
    record = FileSessionRegistry(harness_sessions_dir(home=home)).load(
        request.agent_id, harness=request.harness
    )
    if record is None:
        return _EMPTY
    path = session_file(environ, Path(record.cwd), record.session_id)
    if path is None:
        return _EMPTY

    def load_sent(since: datetime, until: datetime) -> dict[str, list[TimelineItem]]:
        with pool.connection() as conn:
            return sent_inputs(load_sent_rows(conn, request.agent_id, since=since, until=until))

    try:
        window = read_window(
            RecordFile(path),
            limit=request.limit,
            before=request.before,
            policy=ReplyPolicy(request.explicit_visibility, request.delivery_mode),
            render_tool=format_tool_use,
            load_sent=load_sent,
        )
    except FileNotFoundError:
        return _EMPTY
    return HarnessTimelineWindow(
        items=window.timeline_items, has_more=window.has_more, size=window.size
    )


_EMPTY = HarnessTimelineWindow(items=[], has_more=False, size=0)
