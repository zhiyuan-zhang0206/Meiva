# meiva:note
# What the ops daemon says on the wire, over values.

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from base.api_contracts.agent_contract import AgentNotFound
from ops.cluster_status import ClusterStatus
from ops.rpc_schemas import OpEnvelope, OpStatus
from services.agent_runner.agent_ops.ops_plan import (
    OpReply,
    failure_result,
    op_reply,
    read_request,
    unknown_kind,
)


def _body(reply: OpReply) -> dict[str, object]:
    assert reply.content_type == "application/json"
    return json.loads(reply.body)


def test_a_well_formed_request_is_its_envelope() -> None:
    request = read_request(b'{"kind": "config_read", "payload": {"a": 1}, "idempotency_key": "k"}')

    assert isinstance(request, OpEnvelope)
    assert (request.kind, request.payload, request.idempotency_key) == (
        "config_read",
        {"a": 1},
        "k",
    )


def test_a_body_that_is_not_json_is_refused_with_400() -> None:
    request = read_request(b"{not json")

    assert isinstance(request, OpReply)
    assert request.status_code == 400
    assert "invalid JSON body" in str(_body(request)["error"])


@pytest.mark.parametrize("body", [b"[]", b'{"payload": {}}', b'{"kind": 3}'])
def test_a_body_without_a_kind_and_payload_is_refused_with_400(body: bytes) -> None:
    request = read_request(body)

    assert isinstance(request, OpReply)
    assert request.status_code == 400
    assert "body must be {kind: str, payload: dict}" in str(_body(request)["error"])


def test_an_unknown_kind_is_a_failed_result_at_http_200() -> None:
    request = read_request(b'{"kind": "bogus_kind"}')

    assert isinstance(request, OpReply)
    assert request.status_code == 200
    assert _body(request) == {
        "status": "failed",
        "result": {"error": "unknown kind: 'bogus_kind'"},
    }
    assert unknown_kind("bogus_kind") == (OpStatus.FAILED, {"error": "unknown kind: 'bogus_kind'"})


def test_a_finished_operation_is_answered_with_its_status_and_result_at_200() -> None:
    reply = op_reply(OpStatus.COMPLETED, {"paused": False})

    assert reply.status_code == 200
    assert _body(reply) == {"status": "completed", "result": {"paused": False}}


def test_a_failed_operation_is_still_http_200() -> None:
    reply = op_reply(OpStatus.FAILED, {"error": "boom"})

    assert reply.status_code == 200
    assert _body(reply) == {"status": "failed", "result": {"error": "boom"}}


def test_a_value_that_is_not_json_native_degrades_to_its_string() -> None:
    reply = op_reply(OpStatus.COMPLETED, {"at": datetime(2026, 6, 11, 8, 30, tzinfo=UTC)})

    assert _body(reply)["result"] == {"at": "2026-06-11 08:30:00+00:00"}


def test_a_status_dump_keeps_its_datetimes_as_json_strings() -> None:
    status = ClusterStatus(
        machine_name="runner-1",
        serve_gateway=False,
        serve_agent_runner=True,
        paused=False,
        agent_groups=[
            {
                "agent_id": 7,
                "label": "",
                "shells": [
                    {
                        "name": "meiva-agent-7",
                        "created_at": datetime(2026, 6, 11, 8, 30, tzinfo=UTC),
                        "uptime_seconds": 120,
                    }
                ],
            }
        ],
    )

    reply = op_reply(OpStatus.COMPLETED, status.model_dump(mode="json"))

    result = _body(reply)["result"]
    assert isinstance(result, dict)
    assert result["agent_groups"][0]["shells"][0]["created_at"] == "2026-06-11T08:30:00Z"


def test_an_agent_error_fails_with_its_type_detail_and_wire_reason() -> None:
    assert failure_result(AgentNotFound("agent 999 does not exist")) == (
        OpStatus.FAILED,
        {
            "error": "AgentNotFound: agent 999 does not exist",
            "detail": "agent 999 does not exist",
            "reason": "agent_not_found",
        },
    )


def test_any_other_exception_fails_with_its_type_and_message() -> None:
    assert failure_result(ValueError("lifecycle path not recognized: '/x'")) == (
        OpStatus.FAILED,
        {"error": "ValueError: lifecycle path not recognized: '/x'"},
    )
    assert failure_result(RuntimeError("boom")) == (
        OpStatus.FAILED,
        {"error": "RuntimeError: boom"},
    )


def test_a_validation_error_names_the_missing_field() -> None:
    with pytest.raises(ValidationError) as raised:
        OpEnvelope.model_validate({"payload": {}})

    status, result = failure_result(raised.value)

    assert status is OpStatus.FAILED
    assert "kind" in str(result["error"])
