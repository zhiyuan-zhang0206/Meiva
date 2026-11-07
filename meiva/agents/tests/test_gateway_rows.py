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


# meiva:note
# `attached` is the one terminate field whose absence is the normal case: a plain agent has no
# registration row, so a null answers None instead of raising, while a present row must name the
# controller and may carry no expiry.
def test_an_absent_attachment_is_none():
    assert gateway_rows.harness_attachment_from_dict(None) is None


def test_a_present_attachment_reads_the_controller_and_its_expiry():
    assert gateway_rows.harness_attachment_from_dict(
        {"agent_id": 7, "controller_pid": 4242, "expires_at": "2026-11-08T01:00:00+00:00"}
    ) == agents.HarnessAttachment(
        agent_id=7, controller_pid=4242, expires_at="2026-11-08T01:00:00+00:00"
    )


def test_an_attachment_without_an_expiry_is_not_expired_by_default():
    attachment = gateway_rows.harness_attachment_from_dict(
        {"agent_id": 7, "controller_pid": 4242, "expires_at": None}
    )
    assert attachment is not None
    assert attachment.expires_at is None


def test_a_wrong_typed_attachment_names_the_field():
    with pytest.raises(TypeError, match="controller_pid"):
        gateway_rows.harness_attachment_from_dict(
            {"agent_id": 7, "controller_pid": "4242", "expires_at": None}
        )
