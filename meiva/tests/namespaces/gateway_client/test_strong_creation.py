"""Explicit strong creation never downgrades an uncertain intent."""

import httpx
import pytest

from base.agents.context.gateway_client import transport
from base.api_contracts.idempotency import PRINCIPAL_SCOPE, SCOPE_HEADER
from meiva.gateway_client import spawn
from meiva.sdk_surface import process_context
from meiva.tests.fixtures.gateway_wire import GatewayWire, Reply
from meiva.tests.fixtures.gateway_wire import bound_wire as bound_wire
from meiva.tests.fixtures.gateway_wire import gateway_stub as gateway_stub
from meiva.tests.fixtures.gateway_wire import wire as wire

_AGENTS = "/api/keyed/v1/agents"


def _spawn(**kwargs: object) -> int:
    return spawn(
        spawner="user",
        prompt="goal",
        fork_from=None,
        prompt_source="user",
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("bad", [1, "true", None, [True]])
def test_removed_mode_never_submits(bound_wire: GatewayWire, bad: object) -> None:
    with pytest.raises(TypeError, match="require_idempotency"):
        _spawn(require_idempotency=bad, idempotency_key="key")
    assert bound_wire.sent == []


@pytest.mark.parametrize("key", [None, "", "x" * 129, 123])
def test_strong_requires_valid_caller_key(bound_wire: GatewayWire, key: object) -> None:
    with pytest.raises((ValueError, TypeError), match="idempotency key"):
        _spawn(idempotency_key=key)
    assert bound_wire.sent == []


def test_fork_uses_fixed_keyed_admission(bound_wire: GatewayWire) -> None:
    bound_wire.stub.reply("POST", _AGENTS, {"id": 42}, status=201)
    assert (
        spawn(
            spawner="user", prompt=None, fork_from=1, prompt_source="user", idempotency_key="fork"
        )
        == 42
    )
    [sent] = bound_wire.sent
    assert sent.path == _AGENTS
    assert sent.body["fork_from"] == 1
    assert sent.headers[SCOPE_HEADER] == PRINCIPAL_SCOPE


def test_unknown_outcome_is_retried_with_the_same_path_and_key(bound_wire: GatewayWire) -> None:
    bound_wire.stub.reply("POST", _AGENTS, {"id": 42}, status=201)
    bound_wire.fail(httpx.ReadTimeout, delivered=True)
    assert _spawn(idempotency_key="intent") == 42
    first, second = bound_wire.sent
    assert first.intent == second.intent
    assert first.path == _AGENTS
    assert first.headers["idempotency-key"] == "intent"
    assert first.headers[SCOPE_HEADER] == PRINCIPAL_SCOPE
    assert [received.headers["idempotency-key"] for received in bound_wire.stub.received] == [
        "intent",
        "intent",
    ]


@pytest.mark.parametrize("status", [404, 405, 422, 409, 500])
def test_guarded_http_error_never_falls_back(bound_wire: GatewayWire, status: int) -> None:
    bound_wire.stub.reply_in_turn("POST", _AGENTS, [Reply(status, {"detail": "rejected"})])
    with pytest.raises(httpx.HTTPStatusError):
        _spawn(idempotency_key="intent")
    [sent] = bound_wire.sent
    assert sent.path == _AGENTS


def test_connect_retry_keeps_exact_guarded_intent(bound_wire: GatewayWire) -> None:
    bound_wire.stub.reply("POST", _AGENTS, {"id": 42}, status=201)
    bound_wire.fail(httpx.ConnectError)
    assert _spawn(idempotency_key="intent") == 42
    first, second = bound_wire.sent
    assert first.intent == second.intent
    assert len(bound_wire.stub.received) == 1


@pytest.mark.parametrize("key", ["", "x" * 129, 12])
def test_transport_rejects_an_invalid_key_before_the_network(
    bound_wire: GatewayWire, key: object
) -> None:
    with pytest.raises((ValueError, TypeError), match="idempotency key"):
        transport.post(
            _AGENTS,
            idempotency_key=key,  # type: ignore[arg-type]
            context=process_context.current_context(),
        )
    assert bound_wire.sent == []


def test_transport_has_no_scope_override(bound_wire: GatewayWire) -> None:
    with pytest.raises(TypeError):
        transport.post(
            _AGENTS,
            idempotency_scope="legacy",  # type: ignore[call-arg]
            context=process_context.current_context(),
        )
    assert bound_wire.sent == []


class TestSpawn:
    def test_spawn_returns_agent_id(self, bound_wire: GatewayWire):
        bound_wire.stub.reply("POST", _AGENTS, {"id": 42})

        agent_id = spawn(
            spawner="user",
            prompt="hello",
            fork_from=None,
            prompt_source="user",
            idempotency_key="creation",
        )
        assert agent_id == 42
        assert bound_wire.sent[0].body == {
            "spawner": "user",
            "prompt": "hello",
            "prompt_source": "user",
        }

    def test_spawn_without_prompt(self, bound_wire: GatewayWire):
        bound_wire.stub.reply("POST", _AGENTS, {"id": 7})

        agent_id = spawn(
            spawner="agent:1",
            prompt=None,
            fork_from=5,
            prompt_source="agent",
            idempotency_key="fork",
        )
        assert agent_id == 7
        assert bound_wire.sent[0].body == {"spawner": "agent:1", "fork_from": 5}

    def test_spawn_read_timeout_is_retried_under_the_same_key(self, bound_wire: GatewayWire):
        """Spawn is keyed: a ReadTimeout may mean the gateway already created
        the agent, so the retry carries the same key and the gateway replays
        the original creation instead of spawning a twin."""
        bound_wire.stub.reply("POST", _AGENTS, {"id": 7}, status=201)
        bound_wire.fail(httpx.ReadTimeout, delivered=True)

        agent_id = spawn(
            spawner="user",
            prompt="hello",
            fork_from=None,
            prompt_source="user",
            idempotency_key="creation-key",
        )
        assert agent_id == 7
        assert [sent.headers["idempotency-key"] for sent in bound_wire.sent] == [
            "creation-key",
            "creation-key",
        ]

    @pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout])
    def test_spawn_pre_send_error_is_retried(
        self, bound_wire: GatewayWire, error: type[httpx.TransportError]
    ):
        """Connect-family failures happen before the request reaches the
        server, so re-sending a spawn is safe — the retry stays."""
        bound_wire.stub.reply("POST", _AGENTS, {"id": 42})
        bound_wire.fail(error)

        agent_id = spawn(
            spawner="user",
            prompt="hello",
            fork_from=None,
            prompt_source="user",
            idempotency_key="creation",
        )
        assert agent_id == 42
        assert len(bound_wire.sent) == 2
        keys = [sent.headers["idempotency-key"] for sent in bound_wire.sent]
        assert keys[0] == keys[1] and keys[0]

    def test_spawn_retries_a_read_timeout_after_a_connect_error(self, bound_wire: GatewayWire):
        bound_wire.stub.reply("POST", _AGENTS, {"id": 7}, status=201)
        bound_wire.fail(httpx.ConnectError)
        bound_wire.fail(httpx.ReadTimeout, delivered=True)

        assert (
            spawn(
                spawner="user",
                prompt="hello",
                fork_from=None,
                prompt_source="user",
                idempotency_key="creation",
            )
            == 7
        )
        assert len(bound_wire.sent) == 3
