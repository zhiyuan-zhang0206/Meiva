from typing import get_args

import pytest

from agent.nodes import NodeName, node_name


# meiva:note
# `NodeName` is the vocabulary a `Command(goto=...)` accepts; the lookup that turns a hook's routing
# value into one of those targets is held to it, so a node added without a row fails here instead of
# in a turn that cannot route.
def test_the_routing_table_covers_every_graph_node() -> None:
    for name in get_args(NodeName):
        assert node_name(name) == name


@pytest.mark.parametrize("value", ["custom", "custom_target", "x", "", "LLM", 7, None])
def test_an_unknown_routing_target_is_refused(value: object) -> None:
    with pytest.raises(RuntimeError, match="is not a graph node"):
        node_name(value)
