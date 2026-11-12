# meiva:note
# The reply progress of an agent's transcript-sourced timeline (`agent_harness_timeline_progress.replies_seq`): the
# highest timeline seq whose reply has been handed to delivery (or dropped with an event). What delivery still owes is
# the keyed `agent_chat` rows after it. These are the only operations on that table, so the part of the host that
# hands replies over can be given this module and nothing else of the writer's storage.
# meiva:link agent/harness_timeline/README.md

from __future__ import annotations

from dataclasses import dataclass

from psycopg import AsyncConnection

from base.agents.history.timeline_item import TimelineItem
from base.db.rows import aquery_all


@dataclass(frozen=True)
class _ReplyRow:
    seq: int
    item: object


# meiva:note
# One reply the writer committed and delivery still owes: its seq and the item, or, when the stored row does not
# validate as a timeline item (which no retry can change), no item and the name of the validation error's type.
@dataclass(frozen=True)
class OwedReply:
    seq: int
    item: TimelineItem | None
    invalid: str | None


@dataclass(frozen=True)
class UnconfirmedReplies:
    replies: list[OwedReply]
    reach: int
    full: bool


# meiva:note
# Make the agent's reply progress, in the transaction of the writer's first commit, at the end of the timeline as it
# was before that commit: history that was already there (an agent that was on the event path, a fork's copy of its
# parent's timeline) is never the writer's to deliver. Never moves an existing one.
async def ainit_reply_progress(conn: AsyncConnection, agent_id: int) -> None:
    await conn.execute(
        "INSERT INTO agent_harness_timeline_progress (agent_id, replies_seq) "
        "SELECT %s, COALESCE(MAX(seq), -1) FROM agent_harness_messages WHERE agent_id = %s "
        "ON CONFLICT (agent_id) DO NOTHING",
        (agent_id, agent_id),
    )


# meiva:note
# The replies the writer committed (rows it keyed) after the agent's reply progress, oldest first, at most `limit`,
# with the seq the progress can be raised to once they have been handled (the last one read when the read was full,
# otherwise the end of the timeline as it was when the read began; a seq read first, so a row committed meanwhile is
# never passed over). None when the agent has no progress (its writer has not committed yet). One range read after the
# progress: its size is the part of the timeline not yet handled, not the length of the timeline. A row that does not
# validate is returned as such, so it cannot make the whole page fail.
async def aload_unconfirmed_replies(
    conn: AsyncConnection, agent_id: int, *, limit: int
) -> UnconfirmedReplies | None:
    cursor = await conn.execute(
        "SELECT p.replies_seq, (SELECT COALESCE(MAX(seq), -1) FROM agent_harness_messages "
        "WHERE agent_id = %s) FROM agent_harness_timeline_progress p WHERE p.agent_id = %s",
        (agent_id, agent_id),
    )
    head: tuple[object, ...] | None = await cursor.fetchone()
    if head is None:
        return None
    done, end = int(str(head[0])), int(str(head[1]))
    found = await aquery_all(
        conn,
        _ReplyRow,
        "SELECT seq, item FROM agent_harness_messages WHERE agent_id = %s AND seq > %s AND seq <= %s "
        "AND source_key IS NOT NULL AND item->>'kind' = 'agent_chat' ORDER BY seq LIMIT %s",
        (agent_id, done, end, limit),
    )
    replies = [_owed(row) for row in found]
    full = len(replies) == limit
    return UnconfirmedReplies(replies, replies[-1].seq if full else end, full)


def _owed(row: _ReplyRow) -> OwedReply:
    try:
        return OwedReply(row.seq, TimelineItem.model_validate(row.item), None)
    except ValueError as error:
        return OwedReply(row.seq, None, type(error).__name__)


# meiva:note
# Raise the agent's reply progress to `seq`: every reply up to it has been handed to delivery (or dropped with an
# event). Only ever raised, and a call that would not raise it writes nothing.
async def aconfirm_replies(conn: AsyncConnection, agent_id: int, seq: int) -> None:
    await conn.execute(
        "UPDATE agent_harness_timeline_progress SET replies_seq = %s "
        "WHERE agent_id = %s AND replies_seq < %s",
        (seq, agent_id, seq),
    )


# meiva:note
# The delivery mode of the publication already recorded under `source_key`, or None: what a dropped conflicting reply
# is reported against.
async def aload_publication_mode(
    conn: AsyncConnection, agent_id: int, source_key: str
) -> str | None:
    cursor = await conn.execute(
        "SELECT mode FROM agent_publications WHERE agent_id = %s AND source_key = %s",
        (agent_id, source_key),
    )
    row = await cursor.fetchone()
    return None if row is None else str(row[0])
