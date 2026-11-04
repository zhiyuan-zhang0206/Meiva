from __future__ import annotations

import pytest

from meiva import agents
from meiva.agents import gateway_rows


def test_a_wrong_typed_field_names_itself():
    with pytest.raises(TypeError, match="live"):
        agents.machines_from_rows([{"name": "test-host", "description": None, "live": "yes"}])


def test_a_row_that_is_not_an_object_names_its_index():
    with pytest.raises(TypeError, match=r"machines\[0\]"):
        agents.machines_from_rows([["test-host", None, True]])


def test_a_neighbor_row_reads_an_integer_score_as_a_number():
    neighbor = gateway_rows.neighbor_from_row(
        {"agent_id": 7, "label": None, "status": "running", "depth": 1, "score": 0}
    )
    assert neighbor == agents.Neighbor(
        agent_id=7, label=None, status=agents.AgentStatus.RUNNING, depth=1, score=0.0
    )
