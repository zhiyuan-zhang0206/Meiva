# meiva:note
# Retry contracts cross a real loopback listener. A held response makes httpx reach its own
# read timeout after the server received the write; HTTP 503 is an actual server response.
# The test root owns a private configuration, context, HTTP client and held-response release.
from collections.abc import Iterator, Sequence
from threading import Event
from urllib.parse import parse_qs

import httpx
import pytest

from base.agents.context import MeivaContext
from base.agents.context.gateway_client import transport
from base.agents.context.identity import AgentIdentity
from base.api_contracts.agent_contract import GatewayUnavailable
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import ConfigBoot
from meiva.boot import derived_clients
from meiva.gateway_client import use_client
from meiva.sdk_surface import process_context
from meiva.tests.fixtures.gateway_wire import GatewayStub, Received, Reply
from meiva.tests.fixtures.gateway_wire import gateway_stub as gateway_stub


@pytest.fixture
def lost_response(gateway_stub: GatewayStub) -> Iterator[Event]:
    release = Event()
    try:
        yield release
    finally:
        release.set()


@pytest.fixture
def retry_gateway(
    gateway_stub: GatewayStub, lost_response: Event, machine_identity: MachineIdentity
) -> Iterator[GatewayStub]:
    config = ConfigBoot()
    config.set_field("gateway_url", gateway_stub.url)
    config.set_field("gateway_client_max_retries", 3)
    config.set_field("gateway_client_retry_delay_seconds", 0.0)
    context = MeivaContext(
        identity=AgentIdentity(7, True),
        clients=derived_clients(config, machine_identity),
        machine=machine_identity,
    )
    previous = process_context.bound_context()
    process_context.bind_context(context)
    try:
        with (
            httpx.Client(base_url=gateway_stub.url, timeout=0.05, trust_env=False) as client,
            use_client(client),
        ):
            yield gateway_stub
    finally:
        context.clients.close()
        if previous is None:
            process_context.unbind_context()
        else:
            process_context.bind_context(previous)


def _ambiguous_response(
    server: GatewayStub, method: str, path: str, failure: str, release: Event
) -> None:
    if failure == "timeout":
        server.hold(method, path, release)
    else:
        server.reply(method, path, status=503)


def _assert_one_logical_key(requests: Sequence[Received], key: str | None = None) -> None:
    assert len(requests) > 1
    keys = {request.headers["Idempotency-Key"] for request in requests}
    assert len(keys) == 1
    assert key is None or keys == {key}
    assert {request.headers["Idempotency-Scope"] for request in requests} == {"principal-v1"}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/agents/7/restart"),
        ("POST", "/api/schedules/1/restart"),
        ("PATCH", "/api/unknown"),
    ],
)
@pytest.mark.parametrize("failure", ["timeout", "server_error"])
def test_unprotected_write_is_sent_once(
    retry_gateway: GatewayStub, lost_response: Event, method: str, path: str, failure: str
) -> None:
    _ambiguous_response(retry_gateway, method, path, failure, lost_response)

    def send() -> transport.TransportResponse:
        if method == "POST":
            return transport.post(path, context=process_context.current_context())
        return transport.patch(path, context=process_context.current_context())

    if failure == "timeout":
        with pytest.raises(GatewayUnavailable, match="result unknown"):
            send()
    else:
        assert send().status_code == 503
    assert len(retry_gateway.received) == 1


def test_natural_idempotent_write_retries(retry_gateway: GatewayStub) -> None:
    retry_gateway.reply_in_turn("PATCH", "/api/agents/1", [Reply(503, None), Reply(200, None)])
    response = transport.patch(
        "/api/agents/1", {"label": "new"}, context=process_context.current_context()
    )
    assert response.status_code == 200
    assert len(retry_gateway.received) == 2


@pytest.mark.parametrize("failure", ["timeout", "server_error"])
def test_keyed_route_retries_an_ambiguous_outcome_under_one_key(
    retry_gateway: GatewayStub, lost_response: Event, failure: str
) -> None:
    path = "/api/keyed/v1/agents"
    _ambiguous_response(retry_gateway, "POST", path, failure, lost_response)
    if failure == "timeout":
        with pytest.raises(GatewayUnavailable):
            transport.post(
                path, idempotency_key="intent-1", context=process_context.current_context()
            )
    else:
        assert (
            transport.post(
                path, idempotency_key="intent-1", context=process_context.current_context()
            ).status_code
            == 503
        )
    _assert_one_logical_key(retry_gateway.received, "intent-1")


@pytest.mark.parametrize("failure", ["timeout", "server_error"])
def test_keyed_patch_retries_an_ambiguous_outcome_under_the_caller_key(
    retry_gateway: GatewayStub, lost_response: Event, failure: str
) -> None:
    _ambiguous_response(retry_gateway, "PATCH", "/api/tasks/1", failure, lost_response)
    if failure == "timeout":
        with pytest.raises(GatewayUnavailable):
            transport.patch(
                "/api/tasks/1",
                {"priority": "P1"},
                idempotency_key="original",
                context=process_context.current_context(),
            )
    else:
        response = transport.patch(
            "/api/tasks/1",
            {"priority": "P1"},
            idempotency_key="original",
            context=process_context.current_context(),
        )
        assert response.status_code == 503
    _assert_one_logical_key(retry_gateway.received, "original")


def test_keyed_post_and_patch_mint_a_key_when_none_is_pinned(retry_gateway: GatewayStub) -> None:
    retry_gateway.reply("PATCH", "/api/tasks/1", {})
    retry_gateway.reply("POST", "/api/agents/1/system-note", {})
    transport.patch("/api/tasks/1", {"priority": "P1"}, context=process_context.current_context())
    transport.post("/api/agents/1/system-note", {}, context=process_context.current_context())
    assert len(retry_gateway.received) == 2
    for request in retry_gateway.received:
        assert request.headers["Idempotency-Key"]
        assert request.headers["Idempotency-Scope"] == "principal-v1"


@pytest.mark.parametrize("key", ["", "x" * 129, 17])
def test_task_patch_rejects_invalid_caller_key_before_network(
    retry_gateway: GatewayStub, key: object
) -> None:
    retry_gateway.reply("PATCH", "/api/tasks/1", {})
    # meiva:note
    # Raw invalid keys deliberately cross the typed API to exercise rejection before HTTP.
    with pytest.raises((TypeError, ValueError), match="idempotency key"):
        transport.patch(
            "/api/tasks/1",
            idempotency_key=key,  # pyright: ignore[reportArgumentType]
            context=process_context.current_context(),
        )
    assert retry_gateway.received == []


@pytest.mark.parametrize("operation", ["register", "close"])
@pytest.mark.parametrize("failure", ["timeout", "server_error"])
def test_public_page_call_retries_an_ambiguous_effect_under_one_key(
    retry_gateway: GatewayStub, lost_response: Event, operation: str, failure: str
) -> None:
    from meiva.gateway_client import close_page, register_page

    retry_gateway.reply("GET", "/api/agents/7/pages", [{"id": 5, "name": "report"}])
    path = "/api/keyed/v1/agents/7/pages" + ("/report/close" if operation == "close" else "")
    _ambiguous_response(retry_gateway, "POST", path, failure, lost_response)
    error = GatewayUnavailable if failure == "timeout" else httpx.HTTPStatusError
    with pytest.raises(error):
        if operation == "register":
            register_page(7, name="report", port=8001, host="test-host", title=None)
        else:
            close_page(7, "report")
    writes = [request for request in retry_gateway.received if request.method != "GET"]
    _assert_one_logical_key(writes)
    assert {write.method for write in writes} == {"POST"}
    assert {write.path for write in writes} == {path}
    if operation == "close":
        assert {write.body["expected_page_id"] for write in writes} == {5}


def test_close_page_without_an_open_page_sends_no_write(retry_gateway: GatewayStub) -> None:
    from meiva.gateway_client import close_page

    retry_gateway.reply("GET", "/api/agents/7/pages", [])
    assert close_page(7, "report") is False
    assert [request.method for request in retry_gateway.received] == ["GET"]


@pytest.mark.parametrize("operation", ["edit", "dismiss"])
@pytest.mark.parametrize("failure", ["timeout", "server_error"])
def test_fleet_current_notice_call_retries_its_observed_intent_under_one_key(
    retry_gateway: GatewayStub, lost_response: Event, operation: str, failure: str
) -> None:
    from meiva_builtins.plugins.meiva_fleet.plugin import dismiss_notice, edit_notice

    retry_gateway.reply("GET", "/api/notices/open", [{"id": 11}])
    method = "PATCH" if operation == "edit" else "POST"
    path = "/api/agents/7/notices/current" + (
        "/guarded-v1" if operation == "edit" else "/dismiss/guarded-v1"
    )
    _ambiguous_response(retry_gateway, method, path, failure, lost_response)
    error = GatewayUnavailable if failure == "timeout" else httpx.HTTPStatusError
    with pytest.raises(error):
        if operation == "edit":
            edit_notice(title="Original edit")
        else:
            dismiss_notice()
    writes = [request for request in retry_gateway.received if request.method != "GET"]
    _assert_one_logical_key(writes)
    assert {write.method for write in writes} == {method}
    assert {write.path for write in writes} == {path}
    assert {write.body["observed_notice_id"] for write in writes} == {11}


@pytest.mark.parametrize("operation", ["edit", "dismiss"])
def test_fleet_current_notice_call_without_an_open_notice_sends_no_write(
    retry_gateway: GatewayStub, operation: str
) -> None:
    from meiva_builtins.plugins.meiva_fleet.plugin import dismiss_notice, edit_notice

    retry_gateway.reply("GET", "/api/notices/open", [])
    if operation == "edit":
        edit_notice(title="Nothing open")
    else:
        dismiss_notice()
    assert [request.method for request in retry_gateway.received] == ["GET"]
    assert parse_qs(retry_gateway.received[0].query)["agent_id"] == ["7"]


def test_keyed_patch_sends_the_principal_scope_with_the_caller_key(
    retry_gateway: GatewayStub,
) -> None:
    retry_gateway.reply("PATCH", "/api/agents/7/notices/current/guarded-v1", {})
    transport.patch(
        "/api/agents/7/notices/current/guarded-v1",
        {"observed_notice_id": 3, "title": "x"},
        idempotency_key="intent-1",
        context=process_context.current_context(),
    )
    [request] = retry_gateway.received
    assert request.headers["Idempotency-Key"] == "intent-1"
    assert request.headers["Idempotency-Scope"] == "principal-v1"
