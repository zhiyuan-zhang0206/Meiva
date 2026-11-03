# meiva:note
# The turn boundary's `turn_end` record: the halted model call, emitted once, with its reason.
#
# `claim` is the one node that decides whether a graph invocation — one turn — ends. The
# model call that stopped the loop held its record instead of emitting it
# (`agent/graph/llm/node.py`): whether it was the turn's last call is decided here, not
# there. This module turns that held call into the record, with the reason produced at this
# decision point:
#
# - the agent's own declaration, when it made one (validated where it entered the state);
# - `no_input` when nothing more arrived and nothing was declared;
# - no reason at all when the turn continues past the halt, or when a lifecycle command
#   (terminate / restart) ended it — recording `no_input` for those would be a guess about
#   a turn the loop did not end by running out of input.
#
# The held call and any declaration are cleared in the same update, so neither reaches the
# next turn. A declaration made mid-turn survives the claim pass between the code run that
# made it and the model call that follows — only a turn that ends or a halt that is emitted
# clears it.
# meiva:link agent/graph/llm/docs/turn-records.meiva.okf.md

from __future__ import annotations

from langgraph.types import Command

from agent import state as _state
from agent.nodes import END
from base.agents.observation.ended_with import NO_INPUT
from base.log import logger


# meiva:note
# Whether this return ends the turn, rather than continuing it into another round. A
# lifecycle command (terminate / restart) also ends the invocation, but the loop did not end
# that turn by running out of input, so it carries no reason of its own.
def _ends_turn[G](command: Command[G]) -> bool:
    update: dict[str, object] = dict(command.update or {})
    return command.goto == END and not (
        update.get("exit_requested") or update.get("restart_requested")
    )


# meiva:note
# Emit the record a halting model call held, now that this turn's fate is decided. Returns
# `command` untouched when the turn has nothing to settle — the common case, and the reason
# this costs two attribute reads per claim pass. The declaration is spent here: the turn
# ended with it, or the halt it belonged to has just been emitted.
def settle_turn_end[G](state: _state.AgentState, command: Command[G], agent_id: int) -> Command[G]:
    held = state.turn_end_held_seconds
    ending = _ends_turn(command)
    if held is None and not (ending and state.ended_with is not None):
        return command
    update = dict(command.update or {})
    if held is not None:
        ended_with = (state.ended_with or NO_INPUT) if ending else None
        fields: dict[str, object] = {"ended_with": ended_with} if ended_with else {}
        if ended_with and state.ended_with_reason:
            fields["ended_with_reason"] = state.ended_with_reason
        logger.info(
            "turn ended in {duration_seconds:.2f}s ok=True",
            event="turn_end",
            agent_id=agent_id,
            duration_seconds=held,
            ok=True,
            **fields,
        )
        update["turn_end_held_seconds"] = None
    update["ended_with"] = None
    update["ended_with_reason"] = None
    return Command(goto=command.goto, update=update)
