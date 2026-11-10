# meiva:note
# `meiva.self.restart` in the four situations it serves. A native agent that stays native writes
# its own restart row (`test_self_lifecycle.py` runs that against the real database). Every other
# restart is sent to the gateway as the agent's own, to be recorded for the turn boundary: the
# request carries `at_turn_end` and the agent's own source, a refusal reaches the caller before
# anything is recorded, and only the native harness ends its execution afterwards; an external
# harness gets the receipt as the call's result and ends its turn itself.
# meiva:link meiva/self/README.md

from __future__ import annotations

import json

import httpx
import pytest

import meiva
from base.agents.context.identity import ExternalLease
from base.agents.lifecycle import AgentRestart
from base.api_contracts.agent_contract import Harness
from meiva.gateway_client import use_client
from meiva.tests.fixtures.pin_agent import pin_agent

_AGENT = 31


class _Gateway:
    def __init__(self, *, status: int = 200, body: object = None) -> None:
        self.requests: list[httpx.Request] = []
        self._status = status
        self._body = {"status": "enqueued"} if body is None else body

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self._status, json=self._body)

    def sent(self) -> dict[str, object]:
        [request] = self.requests
        assert request.method == "POST"
        assert request.url.path == f"/api/agents/{_AGENT}/restart"
        return json.loads(request.content)


def _attached(harness: Harness) -> None:
    pin_agent(
        _AGENT,
        owns_loop=True,
        harness=harness,
        lease=ExternalLease(agent_id=_AGENT, validate=lambda: _AGENT, config=lambda: None),
    )


def _call(gateway: _Gateway, message: str, overlay: dict[str, object] | None) -> str:
    with (
        httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(gateway)) as http,
        use_client(http),
    ):
        return meiva.self.restart(message, config_overlay=overlay)


def test_a_native_agent_switching_to_an_external_harness_records_the_request_and_ends_its_run() -> (
    None
):
    pin_agent(_AGENT, owns_loop=True)
    gateway = _Gateway()

    with pytest.raises(AgentRestart):
        _call(gateway, "I need a real shell", {"harness": "codex", "llm_model": "gpt-6.1-sol"})

    assert gateway.sent() == {
        "source": f"agent:{_AGENT}",
        "message": "I need a real shell",
        "config_overlay": {"harness": "codex", "llm_model": "gpt-6.1-sol"},
        "at_turn_end": True,
    }


@pytest.mark.parametrize("harness", [Harness.CLAUDE_CODE, Harness.CODEX, Harness.ANTIGRAVITY])
def test_an_external_agent_restarting_itself_gets_a_receipt_and_keeps_running(
    harness: Harness,
) -> None:
    _attached(harness)
    gateway = _Gateway()

    receipt = _call(gateway, "shorter timeout", {"exec_timeout_seconds": 30})

    assert "applied after the current turn ends, not now" in receipt
    assert gateway.sent() == {
        "source": f"agent:{_AGENT}",
        "message": "shorter timeout",
        "config_overlay": {"exec_timeout_seconds": 30},
        "at_turn_end": True,
    }


def test_an_external_agent_switching_harness_sends_the_overlay_as_given() -> None:
    _attached(Harness.CLAUDE_CODE)
    gateway = _Gateway()

    receipt = _call(gateway, "back to native", {"harness": "meiva", "llm_model": "deepseek-flash"})

    assert "your harness becomes 'meiva'" in receipt
    assert gateway.sent()["config_overlay"] == {"harness": "meiva", "llm_model": "deepseek-flash"}


def test_an_external_restart_without_an_overlay_sends_none() -> None:
    _attached(Harness.CODEX)
    gateway = _Gateway()

    _ = _call(gateway, "reload", None)

    assert "config_overlay" not in gateway.sent()


def test_a_refusal_of_the_gateway_reaches_the_caller_and_nothing_else_happens() -> None:
    pin_agent(_AGENT, owns_loop=True)
    gateway = _Gateway(
        status=422,
        body={"detail": "llm_model must be explicitly provided in config_overlay when switching"},
    )

    with pytest.raises(RuntimeError, match=r"refused \(422\): llm_model must be explicitly"):
        _call(gateway, "switch", {"harness": "codex"})


def test_a_gateway_that_does_not_record_the_request_is_an_error_not_a_receipt() -> None:
    _attached(Harness.CODEX)

    with pytest.raises(RuntimeError, match="not recorded"):
        _call(_Gateway(body={"status": "already_terminated"}), "reload", None)


def test_a_name_that_is_not_a_harness_is_refused_before_any_request() -> None:
    pin_agent(_AGENT, owns_loop=True)
    gateway = _Gateway()

    with pytest.raises(ValueError, match="unknown harness"):
        _call(gateway, "switch", {"harness": "claude-code"})

    assert gateway.requests == []


def test_a_launched_script_cannot_restart_its_agent_on_any_route() -> None:
    pin_agent(_AGENT, owns_loop=False)
    gateway = _Gateway()

    with pytest.raises(RuntimeError, match="can only be called from inside an agent process"):
        _call(gateway, "switch", {"harness": "codex", "llm_model": "gpt-6.1-sol"})

    assert gateway.requests == []
