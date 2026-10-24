# meiva:note
# Wire and direct-call contracts for agent lifecycle vocabularies.

from typing import cast

import pytest

from base.agents.messages.inbound import InboundKind, WakeTriggerKind
from base.db import Database
from base.events.live.bus import EventBus


@pytest.mark.parametrize("kind", list(InboundKind))
def test_wake_trigger_wire_accepts_exact_pending_work_subset(kind: InboundKind) -> None:
    from pydantic import ValidationError

    from base.agents.messages.inbound import validate_wake_trigger_kind
    from ops.rpc_schemas import LifecyclePayload

    allowed = {
        InboundKind.CHAT,
        InboundKind.COMPACT_REQUEST,
        InboundKind.SYSTEM_NOTE,
        InboundKind.HEARTBEAT,
    }
    data = {"path": "test", "trigger_inbound_id": 1, "trigger_inbound_kind": kind.value}
    if kind in allowed:
        payload = LifecyclePayload.model_validate(data)
        assert payload.trigger_inbound_kind is kind
        assert payload.model_dump(mode="json")["trigger_inbound_kind"] == kind.value
        assert validate_wake_trigger_kind(kind.value) is kind
    else:
        with pytest.raises(ValidationError):
            LifecyclePayload.model_validate(data)
        with pytest.raises(ValueError):
            validate_wake_trigger_kind(kind.value)


# meiva:note
# The raw boundary refuses a control kind before the database is read: agent 1 does not exist, so
# a read made first would raise `AgentNotFound` instead of this `ValueError`.
def test_direct_wake_rejects_control_kind_before_db_access(
    database: Database, event_bus: EventBus
) -> None:
    from ops.agents.wake import resurrect_agent

    with pytest.raises(ValueError, match="cannot trigger resurrection"):
        resurrect_agent(
            database,
            event_bus,
            10**9,
            resurrected_by="system",
            trigger_inbound_id=1,
            trigger_inbound_kind=cast(WakeTriggerKind, InboundKind.RESTART),
        )


@pytest.mark.parametrize(
    ("model_name", "enum_name"),
    [
        ("RestartAgentResponse", "RestartResult"),
        ("ResurrectAgentResponse", "ResurrectResult"),
        ("TerminateAgentResponse", "TerminateResult"),
    ],
)
def test_lifecycle_result_models_reuse_operation_enums(model_name: str, enum_name: str) -> None:
    from pydantic import ValidationError

    from base.api_contracts import agent_contract
    from ops import rpc_schemas

    model = getattr(rpc_schemas, model_name)
    enum = getattr(agent_contract, enum_name)
    for result in enum:
        response = model.model_validate({"status": result.value})
        assert response.status is result
        assert response.model_dump(mode="json")["status"] == result.value
    with pytest.raises(ValidationError):
        model.model_validate({"status": "unrecognized"})


def test_wake_trigger_schema_keeps_exact_four_wire_values() -> None:
    from ops.rpc_schemas import LifecyclePayload

    alternatives = LifecyclePayload.model_json_schema()["properties"]["trigger_inbound_kind"][
        "anyOf"
    ]
    assert alternatives == [
        {"enum": ["chat", "compact_request", "system_note", "heartbeat"], "type": "string"},
        {"type": "null"},
    ]
