from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest

from base.agents.context import MeivaContext
from meiva import agents
from meiva.gateway_client import use_client


@pytest.fixture
def gateway_requests(sdk_environment: MeivaContext) -> Iterator[list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": 42})

    with (
        httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(capture)) as client,
        use_client(client),
    ):
        yield requests


@pytest.fixture
def no_gateway_http(sdk_environment: MeivaContext) -> Iterator[None]:
    def unexpected(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"invalid input reached HTTP: {request.method} {request.url.path}")

    with (
        httpx.Client(
            base_url="http://gateway", transport=httpx.MockTransport(unexpected)
        ) as client,
        use_client(client),
    ):
        yield


def _body(request: httpx.Request) -> dict[str, object]:
    return json.loads(request.content)


def test_spawn_sends_the_requested_harness(gateway_requests: list[httpx.Request]) -> None:
    assert (
        agents.spawn(
            harness="codex",
            machine="test-machine",
        )
        == 42
    )
    [request] = gateway_requests
    assert _body(request)["harness"] == "codex"


def test_spawn_omits_an_unspecified_harness(gateway_requests: list[httpx.Request]) -> None:
    assert (
        agents.spawn(
            machine="test-machine",
        )
        == 42
    )
    [request] = gateway_requests
    assert "harness" not in _body(request)


def test_fork_omits_an_unspecified_harness_so_the_source_decides(
    gateway_requests: list[httpx.Request],
) -> None:
    assert (
        agents.spawn(
            fork_from=7,
            machine="test-machine",
        )
        == 42
    )
    [request] = gateway_requests
    body = _body(request)
    assert body["fork_from"] == 7
    assert "harness" not in body


def test_spawn_accepts_the_harness_enum(gateway_requests: list[httpx.Request]) -> None:
    assert (
        agents.spawn(
            harness=agents.Harness.ANTIGRAVITY,
            machine="test-machine",
        )
        == 42
    )
    [request] = gateway_requests
    assert _body(request)["harness"] == "antigravity"


def test_spawn_refuses_an_unknown_harness_before_the_request(no_gateway_http: None) -> None:
    with pytest.raises(ValueError, match="unknown harness"):
        agents.spawn(
            harness="claude-code",
            machine="test-machine",
        )


def test_fork_naming_an_external_harness_is_left_to_the_gateway(
    gateway_requests: list[httpx.Request],
) -> None:
    assert agents.spawn(fork_from=7, harness="claude_code", machine="test-machine") == 42
    [request] = gateway_requests
    assert _body(request)["fork_from"] == 7
    assert _body(request)["harness"] == "claude_code"


@pytest.mark.usefixtures("sdk_model_owner")
def test_spawn_accepts_uncatalogued_model_on_external_harness(
    gateway_requests: list[httpx.Request],
) -> None:
    assert (
        agents.spawn(
            harness="antigravity",
            config_overlay={"llm_model": "gpt-oss-120b", "reasoning_effort": "medium"},
            machine="test-machine",
        )
        == 42
    )
    [request] = gateway_requests
    body = _body(request)
    assert body["harness"] == "antigravity"
    assert body["config"] == {"llm_model": "gpt-oss-120b", "reasoning_effort": "medium"}


def test_harness_availability_reports_every_harness() -> None:
    rows = agents.harness_availability()
    assert [row.harness for row in rows] == list(agents.Harness)
    assert all(row.reason for row in rows)
    assert rows[0].status == "available"
    assert rows[0].harness is agents.Harness.MEIVA
    assert rows[0].models == ()


def test_fork_of_an_external_agent_defaults_to_the_sources_machine(
    sdk_environment: MeivaContext,
) -> None:
    requests: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"harness": "codex", "machine": "macmini"})
        return httpx.Response(200, json={"id": 42})

    with (
        httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(answer)) as client,
        use_client(client),
    ):
        assert agents.spawn(fork_from=7) == 42
        assert agents.spawn(fork_from=7, machine="ubuntu") == 42
    posts = [_body(request) for request in requests if request.method == "POST"]
    assert [body["machine"] for body in posts] == ["macmini", "ubuntu"]
    assert sum(request.method == "GET" for request in requests) == 1
