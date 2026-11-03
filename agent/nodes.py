from base.agents.context.external_attachment.nodes import (
    AFTER_EXEC,
    AFTER_INIT,
    BEFORE_EXEC,
    BEFORE_LLM,
    CLAIM,
    END,
    EXEC,
    HARNESS_TURN,
    INIT_CONTEXT,
    LLM,
    NodeName,
)

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
    "node_name",
]

# meiva:note
# Every routing target spelled once more, this time as a lookup: a hook answers `dict[str, object]`
# (its update values are untyped by nature), so the `goto` it routes to comes back as `object` and
# has to become a `Command(goto=...)` target. Looking the name up in this table does that without a
# cast, and a name outside the graph's vocabulary fails here instead of routing a turn to a node
# that does not exist. `agent/tests/test_node_names.py` holds the table to `NodeName`.
_NODE_BY_NAME: dict[str, NodeName] = {
    CLAIM: CLAIM,
    BEFORE_LLM: BEFORE_LLM,
    LLM: LLM,
    BEFORE_EXEC: BEFORE_EXEC,
    EXEC: EXEC,
    AFTER_EXEC: AFTER_EXEC,
    AFTER_INIT: AFTER_INIT,
    INIT_CONTEXT: INIT_CONTEXT,
    HARNESS_TURN: HARNESS_TURN,
    END: END,
}


# meiva:note
# The graph node `value` names; `RuntimeError` when it names none.
def node_name(value: object) -> NodeName:
    found = _NODE_BY_NAME.get(value) if isinstance(value, str) else None
    if found is None:
        raise RuntimeError(f"hook routed to {value!r}, which is not a graph node")
    return found
