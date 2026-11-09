from typing import Literal

# meiva:note
# Each constant is narrowed to its own Literal so narrow goto types like
# `Command[Literal["before_turn", "exec", "claim"]]` accept `goto=BEFORE_TURN`;
# annotating with the broader `NodeName` would fail narrow Command type checks.
# meiva:link agent/graph/docs/graph.md
CLAIM: Literal["claim"] = "claim"
BEFORE_TURN: Literal["before_turn"] = "before_turn"
LLM: Literal["llm"] = "llm"
EXEC: Literal["exec"] = "exec"
INIT_CONTEXT: Literal["init_context"] = "init_context"
# meiva:note
# The whole turn of a non-native harness: before_turn routes here instead of llm when
# the agent runs on an external CLI, because that CLI owns the model call, the tool
# calls and the transcript in one loop. The native nodes never run under it.
# meiva:link agent/graph/docs/harness-turn.md
HARNESS_TURN: Literal["harness_turn"] = "harness_turn"

# meiva:note
# Spelled out rather than imported from langgraph: `langgraph.graph.END` is inferred
# as `LiteralString`, which a narrow `Command[...]` type argument does not accept.
END: Literal["__end__"] = "__end__"

# meiva:note
# Every legal `Command(goto=...)` target. END is included because any node or hook
# may route to graph termination (for example, a terminate co-batched with other work).
# meiva:link agent/graph/docs/graph.md
NodeName = Literal[
    "init_context",
    "claim",
    "before_turn",
    "llm",
    "exec",
    "harness_turn",
    "__end__",
]

__all__ = [
    "BEFORE_TURN",
    "CLAIM",
    "END",
    "EXEC",
    "HARNESS_TURN",
    "INIT_CONTEXT",
    "LLM",
    "NodeName",
]
