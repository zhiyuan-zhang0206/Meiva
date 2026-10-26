# meiva:note
# Explicit checkpoint resources passed through one hosted admission.

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
