from typing import Literal

# meiva:note
# Each constant is narrowed to its own Literal so narrow goto types like
# `Command[Literal["before_exec", "after_exec"]]` accept `goto=BEFORE_EXEC`;
# annotating with the broader `NodeName` would fail narrow Command type checks.
# meiva:link agent/graph/docs/graph.meiva.okf.md
CLAIM: Literal["claim"] = "claim"
BEFORE_LLM: Literal["before_llm"] = "before_llm"
LLM: Literal["llm"] = "llm"
BEFORE_EXEC: Literal["before_exec"] = "before_exec"
EXEC: Literal["exec"] = "exec"
AFTER_EXEC: Literal["after_exec"] = "after_exec"
AFTER_INIT: Literal["after_init"] = "after_init"
INIT_CONTEXT: Literal["init_context"] = "init_context"
# meiva:note
# The whole turn of a non-native harness: claim routes here instead of before_llm when
# the agent runs on an external CLI, because that CLI owns the model call, the tool
# calls and the transcript in one loop. The native nodes never run under it.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
HARNESS_TURN: Literal["harness_turn"] = "harness_turn"

# meiva:note
# Spelled out rather than imported from langgraph: `langgraph.graph.END` is inferred
# as `LiteralString`, which a narrow `Command[...]` type argument does not accept.
END: Literal["__end__"] = "__end__"

# meiva:note
# Every legal `Command(goto=...)` target. END is included because any node or hook
# may route to graph termination (for example, a terminate co-batched with other work).
# meiva:link agent/graph/docs/graph.meiva.okf.md
NodeName = Literal[
    "claim",
    "before_llm",
    "llm",
    "before_exec",
    "exec",
    "after_exec",
    "after_init",
    "init_context",
    "harness_turn",
    "__end__",
]

__all__ = [
    "AFTER_EXEC",
    "AFTER_INIT",
    "BEFORE_EXEC",
    "BEFORE_LLM",
    "CLAIM",
    "END",
    "EXEC",
    "HARNESS_TURN",
    "INIT_CONTEXT",
    "LLM",
    "NodeName",
]
