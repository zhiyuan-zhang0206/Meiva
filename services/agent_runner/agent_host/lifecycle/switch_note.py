# meiva:note
# The history note a harness switch leaves in the native checkpoint when an agent leaves
# the built-in `meiva` runtime, so the timeline shows where the native history ended.
# External harnesses keep their history in CLI records and receive the switch note on
# arrival instead, so leaving an external harness writes nothing here. The note is
# decoration: a checkpoint that cannot take it is logged and the switch carries on.
# meiva:link agent/graph/docs/harness-turn.md

import logging
from dataclasses import dataclass

from base.agents.messages.timestamp_policy import TimestampPolicy

_log = logging.getLogger(__name__)


# meiva:note
# The checkpoint graph the outgoing switch note lands in and the clock that stamps it.
@dataclass(frozen=True)
class SwitchHistory:
    graph: object | None
    timestamps: TimestampPolicy


# meiva:note
# Append the switch note to the native checkpoint when leaving `meiva`; failures are logged, never raised.
async def record_switch_note(
    history: SwitchHistory,
    agent_id: int,
    *,
    old_harness: str,
    new_harness: str,
    source: str,
    message: str,
) -> None:
    from agent.graph.node_log import SNAPSHOT_CURSOR
    from agent.messages import NoteTag, system_note_message
    from base.agents.messages.lifecycle_text import by_who

    if old_harness == new_harness:
        return
    SNAPSHOT_CURSOR.pop(agent_id, None)
    if old_harness != "meiva" or history.graph is None:
        return
    now = history.timestamps.require_now()
    note_content = (
        f"Harness switched from '{old_harness}' to '{new_harness}' by {by_who(source)}: {message}"
    )
    note = system_note_message(
        inputs=history.timestamps,
        content=note_content,
        tag=NoteTag.LIFECYCLE_RESTART,
        picked_up_at=now,
        created_at=now,
    )
    try:
        await history.graph.aupdate_state(  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
            {"configurable": {"thread_id": str(agent_id)}}, {"messages": [note]}
        )
    except Exception as exc:
        _log.warning(
            "[health] failed to record harness switch note in meiva state for agent %s: %s",
            agent_id,
            exc,
        )
