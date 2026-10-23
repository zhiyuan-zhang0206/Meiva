"""Bounded same-key message delivery, without a sender journal."""

from pathlib import Path

import httpx
import pytest

from base.api_contracts.agent_contract import GatewayUnavailable
from meiva.gateway_client import send_message, spawn
from meiva.tests.fixtures.gateway_wire import GatewayWire, Reply
from meiva.tests.fixtures.gateway_wire import bound_wire as bound_wire
from meiva.tests.fixtures.gateway_wire import gateway_stub as gateway_stub
from meiva.tests.fixtures.gateway_wire import wire as wire

_MESSAGES = "/api/agents/7/messages"


@pytest.mark.parametrize(
    ("error", "delivered"),
    [
        (httpx.WriteTimeout, False),
        (httpx.ReadError, True),
        (httpx.RemoteProtocolError, True),
    ],
)
def test_keyed_spawn_retries_uncertain_transport_error_under_one_key(
    bound_wire: GatewayWire, error: type[httpx.TransportError], delivered: bool
) -> None:
    bound_wire.fail(error, delivered=delivered, count=3)
    with pytest.raises(GatewayUnavailable, match=r"after \d+ retries"):
        spawn(
            spawner="user",
            prompt="hello",
            fork_from=None,
            prompt_source="user",
            idempotency_key="creation",
        )
    assert len(bound_wire.sent) > 1
    assert {sent.headers["idempotency-key"] for sent in bound_wire.sent} == {"creation"}


def test_transient_send_retries_one_key_and_has_no_journal(
    bound_wire: GatewayWire, unit_home: Path
) -> None:
    bound_wire.stub.reply_in_turn(
        "POST", _MESSAGES, [Reply(503, {}), Reply(201, {"status": "enqueued"})]
    )
    send_message(7, content="hello", source="user")
    keys = [sent.headers["idempotency-key"] for sent in bound_wire.sent]
    assert len(keys) == 2 and len(set(keys)) == 1
    assert not (unit_home / "state" / "delivery-outbox").exists()


def test_separate_calls_keep_separate_logical_messages(bound_wire: GatewayWire) -> None:
    bound_wire.stub.reply("POST", _MESSAGES, {"status": "enqueued"}, status=201)
    send_message(7, content="hello", source="user")
    send_message(7, content="hello", source="user")
    keys = [sent.headers["idempotency-key"] for sent in bound_wire.sent]
    assert len(set(keys)) == 2


def test_unknown_server_error_is_exposed_once(bound_wire: GatewayWire) -> None:
    bound_wire.stub.reply("POST", _MESSAGES, {"detail": "unknown"}, status=500)
    with pytest.raises(httpx.HTTPStatusError):
        send_message(7, content="hello", source="user")
    assert len(bound_wire.sent) == 1


def test_permanent_failure_is_not_retried(bound_wire: GatewayWire) -> None:
    bound_wire.stub.reply("POST", _MESSAGES, {"detail": "invalid input"}, status=422)
    with pytest.raises(httpx.HTTPStatusError):
        send_message(7, content="hello", source="user")
    assert len(bound_wire.sent) == 1


@pytest.mark.parametrize(
    ("error", "delivered"), [(httpx.ReadTimeout, True), (httpx.ConnectError, False)]
)
def test_message_transport_retry_exhausts_one_stable_key(
    bound_wire: GatewayWire, error: type[httpx.TransportError], delivered: bool
) -> None:
    bound_wire.fail(error, delivered=delivered, count=3)
    with pytest.raises(GatewayUnavailable):
        send_message(7, content="hello", source="user")
    keys = [sent.headers["idempotency-key"] for sent in bound_wire.sent]
    assert 1 < len(keys) <= 4 and len(set(keys)) == 1
