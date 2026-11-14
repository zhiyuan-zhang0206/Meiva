# meiva:note
# What a turn records when it dies before its first word.
#
# A claimed inbound whose fate cannot be established is consumed rather than redelivered, so a turn
# that faulted before producing any text records a registered event naming only the rows actually
# retired that way: one the harness's record showed in the context, or that went back to the queue
# to be delivered again, is not lost. Only a turn that produced nothing is recorded -- one that
# already streamed text showed the reader something.
# meiva:link agent/graph/docs/harness-turn.md

from __future__ import annotations

from collections.abc import Sequence

from base.agents.context import MeivaContext
from base.log import logger

__all__ = ["report_lost_turn"]


def report_lost_turn(
    ctx: MeivaContext,
    agent_id: int,
    inbound_ids: Sequence[int],
    turn_key: str,
    error: BaseException,
) -> None:
    reason = f"{type(error).__name__}: {error}"
    logger.warning(
        "harness turn faulted before producing output",
        event="turn_fault_lost_inbound",
        agent_id=agent_id,
        harness=ctx.harness.value,
        turn_key=turn_key,
        inbound_ids=inbound_ids,
        reason=reason,
    )
