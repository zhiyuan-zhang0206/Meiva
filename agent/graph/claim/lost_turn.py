# meiva:note
# What a turn leaves behind when it dies before its first word.
#
# The queue's at-most-once rule consumes a claimed inbound and never redelivers it
# (agent/db/finalize_claimed_inbounds says so on purpose), so without this a turn that faulted
# before producing any text is indistinguishable from the user never having said anything. The loss
# is recorded twice: a system note in the conversation, worded so it cannot be read as a reply, and
# a registered event so the count is queryable instead of living in someone's memory. Only a turn
# that produced nothing is recorded -- one that already streamed text showed the reader something.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime

from langchain_core.messages import BaseMessage

from agent.messages import NoteTag, system_note_message
from base.agents.context import MeivaContext
from base.host.boundary.json_values import is_object_list
from base.log import logger

__all__ = ["lost_turn_note", "pending_inbound_ids", "report_lost_turn"]


# meiva:note
# The ids the claim recorded for this turn: the note names them so a reader can point at the row.
def pending_inbound_ids(pending_input: Mapping[str, object] | None) -> list[int]:
    raw = (pending_input or {}).get("inbound_ids")
    if not is_object_list(raw):
        return []
    return [value for value in raw if isinstance(value, int) and not isinstance(value, bool)]


# meiva:note
# The note says three things and no fourth: the message was not answered, which rows were consumed,
# and that they will not come back.
def lost_turn_note(
    inbound_ids: Sequence[int], turn_key: str, reason: str, *, now: datetime
) -> BaseMessage:
    labelled = ", ".join(str(value) for value in inbound_ids) or "none recorded"
    return system_note_message(
        content=(
            "This message was not answered: the turn failed before producing any output. "
            "The queue has already consumed it and will not deliver it again.\n\n"
            f"turn: {turn_key}\n"
            f"inbound: {labelled}\n"
            f"failure: {reason}"
        ),
        tag=NoteTag.TURN_LOST,
        picked_up_at=now,
        created_at=now,
    )


async def report_lost_turn(
    ctx: MeivaContext,
    agent_id: int,
    pending_input: Mapping[str, object] | None,
    turn_key: str,
    error: BaseException,
    *,
    append: Callable[[MeivaContext, int, Sequence[BaseMessage]], Awaitable[None]],
) -> None:
    inbound_ids = pending_inbound_ids(pending_input)
    reason = f"{type(error).__name__}: {error}"
    now = ctx.require_clock().now()
    try:
        await append(ctx, agent_id, [lost_turn_note(inbound_ids, turn_key, reason, now=now)])
    except Exception:
        # meiva:note
        # Recording the loss must not become a second failure: the caller raises the original fault
        # next, and it has to arrive intact.
        logger.opt(exception=True).warning(
            "could not record the lost turn's note",
            event="turn_fault_note_failed",
            agent_id=agent_id,
            turn_key=turn_key,
        )
    logger.warning(
        "harness turn faulted before producing output",
        event="turn_fault_lost_inbound",
        agent_id=agent_id,
        harness=ctx.harness.value,
        turn_key=turn_key,
        inbound_ids=inbound_ids,
        reason=reason,
    )
