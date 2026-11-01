import pytest
from pydantic import BaseModel, ValidationError

from ops.rpc_schemas import (
    LaunchAgentRequest,
    RestartAgentRequest,
    ResurrectAgentRequest,
    SpawnAgentRequest,
    TerminateAgentRequest,
)

_SCHEMAS: list[tuple[type[BaseModel], str, dict[str, object]]] = [
    (SpawnAgentRequest, "prompt_source", {"prompt": "hello"}),
    (ResurrectAgentRequest, "resurrected_by", {}),
    (RestartAgentRequest, "source", {"message": "restarting"}),
    (TerminateAgentRequest, "source", {"force": True}),
]


@pytest.mark.parametrize(
    "source", ["external_agent:codex:run-42", "external_agent:claude_code", "user"]
)
@pytest.mark.parametrize(("schema", "field", "other"), _SCHEMAS)
def test_declared_source_is_accepted(
    schema: type[BaseModel], field: str, other: dict[str, object], source: str
) -> None:
    assert schema.model_validate(other | {field: source}).model_dump()[field] == source


@pytest.mark.parametrize(
    "source",
    ["external_agent:", "external_agent:a:b:c", "external_agent:bad name", "unknown:legacy"],
)
@pytest.mark.parametrize(("schema", "field", "other"), _SCHEMAS)
def test_malformed_declaration_is_rejected_before_dispatch(
    schema: type[BaseModel], field: str, other: dict[str, object], source: str
) -> None:
    with pytest.raises(ValidationError):
        schema.model_validate(other | {field: source})


@pytest.mark.parametrize(
    ("schema", "extra"),
    [(TerminateAgentRequest, {}), (RestartAgentRequest, {"message": "restarting"})],
)
@pytest.mark.parametrize("reason", ["machine-pause", "incident-operator", "self"])
def test_legacy_lifecycle_audit_reason_is_not_chat_envelope_grammar(
    schema: type[BaseModel], extra: dict[str, object], reason: str
) -> None:
    assert schema.model_validate(extra | {"source": reason}).model_dump()["source"] == reason


# meiva:note
# #8092 made the restart message mandatory on every entry point. A restart without one is refused at
# the wire, and the error names `message`: a payload rejected on some other field would send the
# caller looking in the wrong place.
def test_restart_without_a_message_is_refused_at_the_message_field() -> None:
    with pytest.raises(ValidationError) as refused:
        RestartAgentRequest.model_validate({"source": "user"})

    assert [error["loc"] for error in refused.value.errors()] == [("message",)]


@pytest.mark.parametrize("blank", ["", "   "])
def test_restart_refuses_a_blank_message_at_the_message_field(blank: str) -> None:
    with pytest.raises(ValidationError) as refused:
        RestartAgentRequest.model_validate({"source": "user", "message": blank})

    assert ("message",) in [error["loc"] for error in refused.value.errors()]


@pytest.mark.parametrize("field", ["prompt", "prompt_source", "label"])
def test_launch_rejects_retired_gateway_birth_fields(field: str) -> None:
    with pytest.raises(ValidationError) as refused:
        LaunchAgentRequest.model_validate(
            {
                "agent_id": 42,
                "launch_attempt_id": "00000000-0000-0000-0000-000000000001",
                field: "hello",
            }
        )
    assert [(error["loc"], error["type"]) for error in refused.value.errors()] == [
        ((field,), "extra_forbidden")
    ]
