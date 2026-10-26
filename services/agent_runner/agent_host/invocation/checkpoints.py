# meiva:note
# Explicit checkpoint resources and pure completion decisions passed through one hosted admission.

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph

from base.api_contracts.agent_contract import ReconstructionScope


# meiva:note
# A scoped read view over the host's original saver and compiled graph.
@dataclass(frozen=True)
class TurnCheckpoints:
    saver: AsyncPostgresSaver
    graph: CompiledStateGraph[Any, Any, Any, Any]
    reconstruction: ReconstructionScope[AsyncPostgresSaver] | None = None

    def bind(self, scope: ReconstructionScope[AsyncPostgresSaver] | None) -> "TurnCheckpoints":
        if scope is None:
            return self
        reader = scope.reader()
        # meiva:note
        # LangGraph's public copy preserves nodes, channels, compile options and
        # serializer allowlists. Only this admission's checkpointer changes.
        return TurnCheckpoints(reader, self.graph.copy({"checkpointer": reader}), scope)


# meiva:note
# Whether a completed graph invocation requested a hosted lifecycle apply (restart or terminate).
def lifecycle_return_requested(result: Mapping[str, object]) -> bool:
    return bool(result["exit_requested"] or result["restart_requested"])


# meiva:note
# After a restart claim `halted` is the claim's verdict on the agent it found: set, it was
# idle; clear, its turn was unfinished and the apply queues the note that continues it.
# meiva:link agent/graph/docs/turn-interrupts.meiva.okf.md
def restart_resumes_turn(result: Mapping[str, object]) -> bool:
    return bool(result["restart_requested"]) and not bool(result["halted"])


# meiva:note
# Whether a committed lifecycle command queued a continuation note that this host's scheduler
# should wake immediately.
# meiva:link services/agent_runner/agent_host/lifecycle/docs/graceful-exit.meiva.okf.md
def should_wake_resumed(kind: str | None, *, resume_turn: bool) -> bool:
    return kind == "restart" and resume_turn
