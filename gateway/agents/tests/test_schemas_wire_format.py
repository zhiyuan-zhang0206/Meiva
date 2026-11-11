from datetime import UTC, datetime

from pydantic import BaseModel, TypeAdapter

from gateway.agents.schemas import (
    AgentRow,
    QuestionItem,
)

_WIRE = TypeAdapter(dict[str, object])


# meiva:note
# The JSON a model puts on the wire, decoded back into an object.
def _wire(model: BaseModel) -> dict[str, object]:
    return _WIRE.validate_json(model.model_dump_json())


def test_agent_row_wire_shape() -> None:
    from base.api_contracts.agent_contract import AgentStatus, Harness, LivenessState

    t = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
    m = AgentRow(
        agent_id=1,
        spawner="user",
        fork_source_agent_id=None,
        fork_source_checkpoint_id=None,
        status=AgentStatus.IDLING,
        harness=Harness.MEIVA,
        spawned_at=t,
        last_active_at=t,
        last_inbound_at=t,
        label="agent-1",
        machine="test-machine",
        supports_vision=True,
        liveness_state=LivenessState.ONLINE,
        last_probe_at=None,
        questions_awaiting_response=[],
        has_unread_user_messages=False,
        heartbeat_paused_until=None,
    )
    d = _wire(m)
    assert d["agent_id"] == 1
    assert d["label"] == "agent-1"
    assert d["spawner"] == "user"
    assert d["harness"] == "meiva"
    assert d["supports_vision"] is True
    assert d["questions_awaiting_response"] == []


def test_question_item_wire_shape() -> None:
    t = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
    m = QuestionItem(
        id=5,
        agent_id=3,
        agent_label="agent-3",
        title="deploy?",
        content="A) yes\nB) no",
        created_at=t,
        question={"type": "text"},
        resolved_at=None,
        resolution=None,
        reply=None,
    )
    d = _wire(m)
    assert set(d.keys()) == {
        "id",
        "agent_id",
        "agent_label",
        "title",
        "content",
        "created_at",
        "resolved_at",
        "resolution",
        "reply",
        "question",
        "answer",
        "source_verified_by",
    }
    assert d["id"] == 5
    assert d["agent_label"] == "agent-3"
    assert d["resolved_at"] is None
    assert d["resolution"] is None
    assert d["reply"] is None
    assert d["question"] is None
    assert d["answer"] is None
