# meiva:note
# The history note a harness switch leaves in the store the agent is leaving, so the timeline the
# frontend shows keeps the switch where the old history ends. The native checkpoint and the
# external display store are separate stores: leaving the native harness appends the note to the
# agent's checkpoint, arriving on it appends a system marker to the harness display store, and a
# switch between two external harnesses writes nothing here (they share one store and the first
# turn's restart note is the marker). The note is decoration: a store that cannot take it is
# logged and the switch carries on.
# meiva:link agent/graph/docs/harness-turn.md

import logging
from dataclasses import dataclass
from typing import TypeGuard

from psycopg_pool import AsyncConnectionPool

from base.api_contracts.agent_contract import reads_timeline_from_record
from base.clock import Clock

_log = logging.getLogger(__name__)


def _is_pool(value: object) -> TypeGuard[AsyncConnectionPool]:
    return isinstance(value, AsyncConnectionPool)


# meiva:note
# The stores the note can land in and the clock that stamps it, as values the caller read. `graph`
# is the compiled graph over the agent's checkpoint saver; `pool` the pool the display store is
# written through.
@dataclass(frozen=True)
class SwitchHistory:
    graph: object | None
    pool: object | None
    clock: Clock


# meiva:note
# Append the switch note to the store the agent is leaving; failures are logged, never raised.
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
    now = history.clock.now()
    ts_prefix = f"[{now.strftime('%Y-%m-%d %H:%M:%S UTC')}] "
    note_content = (
        f"{ts_prefix}Harness switched from '{old_harness}' to '{new_harness}' "
        f"by {by_who(source)}: {message}"
    )
    if old_harness == "meiva" and history.graph is not None:
        note = system_note_message(
            content=note_content, tag=NoteTag.LIFECYCLE_RESTART, picked_up_at=now, created_at=now
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
    elif (
        new_harness == "meiva"
        and _is_pool(history.pool)
        and not reads_timeline_from_record(old_harness)
    ):
        from base.agents.history.harness_messages import aappend_harness_timeline_items
        from base.agents.history.timeline_item import TimelineItem
        from base.db.transaction import async_write_transaction

        note_item = TimelineItem(
            item_id="0.0",
            kind="system_marker",
            source="lifecycle_restart",
            payload=note_content,
            created_at=now.isoformat(),
        )
        try:
            async with async_write_transaction(history.pool) as conn:
                await aappend_harness_timeline_items(conn, agent_id, old_harness, [note_item])
        except Exception as exc:
            _log.warning(
                "[health] failed to record harness switch note in harness_messages for agent %s: %s",
                agent_id,
                exc,
            )
