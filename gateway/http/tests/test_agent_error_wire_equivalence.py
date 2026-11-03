# meiva:note
# SDK ↔ Gateway error wire-equivalence.
#
# Locks in the "server raise X → handler encodes typed envelope + reason → SDK parse → reconstruct X"
# end-to-end invariant.
#
# Every MeivaAgentError subclass is parametrized to run two assertions:
#   1. **handler encoding correct**: register handler on an isolated FastAPI app + a synthetic
#      endpoint that raises cls; TestClient hits it; checks the typed envelope plus
#      body.reason==cls.reason / body.detail matches the raised message
#   2. **SDK reconstruction correct**: feed a synthetic httpx.Response to gateway_client.
#      raise_from_response; checks the raised exception is the same cls + same message
#
# When adding a new MeivaAgentError subclass, must also add it to EXCEPTION_BY_REASON — this test
# auto-parametrizes and won't miss coverage.

from __future__ import annotations

import json
import random

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from base.agents.context.gateway_client.transport import raise_from_response
from base.api_contracts.agent_contract import (
    EXCEPTION_BY_REASON,
    AgentLaunchFailed,
    ErrorReason,
    MeivaAgentError,
)
from gateway.http.error_handlers import meiva_agent_error_handler
from gateway.http.middleware.error_envelope import request_trace_middleware

# meiva:note
# The decoded JSON body of a response, keys checked as strings.
_BODY = TypeAdapter(dict[str, object])


# meiva:note
# A fresh seeded generator for each use, so no test sees another's draws.
def _rng() -> random.Random:
    return random.Random(0)  # noqa: S311


# meiva:note
# FastAPI types an exception handler as taking `Exception`; ours takes the `MeivaAgentError`
# subtype it is registered for, so this adapter narrows what dispatch by registered type guarantees.
async def _handle_agent_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, MeivaAgentError)
    return await meiva_agent_error_handler(request, exc)


# meiva:note
# Server-side: handler encodes the full envelope plus SDK wire reason.
@pytest.mark.parametrize(
    ("reason", "cls"),
    list(EXCEPTION_BY_REASON.items()),
    ids=[r.value for r in EXCEPTION_BY_REASON],
)
def test_handler_emits_expected_wire(reason: ErrorReason, cls: type[MeivaAgentError]) -> None:
    app = FastAPI()
    app.middleware("http")(request_trace_middleware(_rng()))
    app.add_exception_handler(MeivaAgentError, _handle_agent_error)

    @app.get("/_raise")
    def _raise() -> None:
        raise cls("forced wire-test message")

    with TestClient(app) as client:
        resp = client.get("/_raise")

    assert resp.status_code == cls.http_status
    body = _BODY.validate_json(resp.content)
    assert body["type"] == "about:blank"
    assert body["code"] == reason.value
    assert body["status"] == cls.http_status
    assert body["reason"] == reason
    assert body["detail"] == "forced wire-test message"
    assert body["retryable"] is False
    assert isinstance(body["trace_id"], str) and body["trace_id"]


# meiva:note
# client-side: feed synthetic wire body to raise_from_response; it reconstructs same cls + message.
@pytest.mark.parametrize(
    ("reason", "cls"),
    list(EXCEPTION_BY_REASON.items()),
    ids=[r.value for r in EXCEPTION_BY_REASON],
)
def test_sdk_reconstructs_from_wire(reason: ErrorReason, cls: type[MeivaAgentError]) -> None:
    body = {"detail": "forced wire-test message", "reason": reason.value}
    resp = httpx.Response(
        status_code=cls.http_status,
        content=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
    )
    with pytest.raises(cls, match="forced wire-test message"):
        raise_from_response(resp)


def test_sdk_launch_failure_retains_committed_identity_and_state() -> None:
    body = {
        "detail": "created but launch failed",
        "reason": "agent_launch_failed",
        "agent_id": 123,
        "state": {"status": "idling", "availability": {"reason": "launch_unreachable"}},
        "retry_launch_path": "/api/agents/123/retry-launch",
    }
    response = httpx.Response(502, json=body)
    with pytest.raises(AgentLaunchFailed) as raised:
        raise_from_response(response)
    assert raised.value.agent_id == 123
    assert raised.value.state == body["state"]
    assert raised.value.retry_launch_path == body["retry_launch_path"]
