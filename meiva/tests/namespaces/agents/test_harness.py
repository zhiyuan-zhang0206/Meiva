from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any, cast

import httpx
import pytest

from base.agents.context import MeivaContext
from base.packages.plugins.plugin_errors import InvalidConfigOverlay
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


def test_spawn_sends_the_requested_harness_inside_the_overlay(
    gateway_requests: list[httpx.Request],
) -> None:
    assert agents.spawn(config_overlay={"harness": "codex"}, machine="test-machine") == 42
    [request] = gateway_requests
    body = _body(request)
    assert body["config"] == {"harness": "codex"}
    assert "harness" not in {key for key in body if key != "config"}


def test_spawn_omits_an_unspecified_harness(gateway_requests: list[httpx.Request]) -> None:
    assert agents.spawn(machine="test-machine") == 42
    [request] = gateway_requests
    assert "harness" not in _body(request)
    assert "config" not in _body(request)


def test_fork_omits_an_unspecified_harness_so_the_source_decides(
    gateway_requests: list[httpx.Request],
) -> None:
    assert agents.spawn(fork_from=7, machine="test-machine") == 42
    [request] = gateway_requests
    body = _body(request)
    assert body["fork_from"] == 7
    assert "harness" not in body
    assert "config" not in body


def test_spawn_accepts_every_harness_name(gateway_requests: list[httpx.Request]) -> None:
    for harness in agents.Harness:
        agents.spawn(config_overlay={"harness": harness.value}, machine="test-machine")
    assert [_body(request)["config"] for request in gateway_requests] == [
        {"harness": harness.value} for harness in agents.Harness
    ]


@pytest.mark.parametrize("bad", ["claude-code", "", None, 7, agents.Harness])
def test_spawn_refuses_an_unknown_harness_before_the_request(
    no_gateway_http: None, bad: object
) -> None:
    with pytest.raises(InvalidConfigOverlay, match="overlay key 'harness'"):
        agents.spawn(config_overlay={"harness": bad}, machine="test-machine")


def test_spawn_has_no_harness_parameter(no_gateway_http: None) -> None:
    with pytest.raises(TypeError, match="harness"):
        cast("Any", agents.spawn)(harness="codex", machine="test-machine")


def test_fork_naming_an_external_harness_is_left_to_the_gateway(
    gateway_requests: list[httpx.Request],
) -> None:
    assert (
        agents.spawn(fork_from=7, config_overlay={"harness": "claude_code"}, machine="test-machine")
        == 42
    )
    [request] = gateway_requests
    assert _body(request)["fork_from"] == 7
    assert _body(request)["config"] == {"harness": "claude_code"}


@pytest.mark.usefixtures("sdk_model_owner")
def test_spawn_accepts_uncatalogued_model_on_external_harness(
    gateway_requests: list[httpx.Request],
) -> None:
    assert (
        agents.spawn(
            config_overlay={
                "harness": "antigravity",
                "llm_model": "gpt-oss-120b",
                "reasoning_effort": "medium",
            },
            machine="test-machine",
        )
        == 42
    )
    [request] = gateway_requests
    assert _body(request)["config"] == {
        "harness": "antigravity",
        "llm_model": "gpt-oss-120b",
        "reasoning_effort": "medium",
    }


def test_harness_availability_reports_every_harness() -> None:
    rows = agents.harness_availability()
    assert [row.harness for row in rows] == list(agents.Harness)
    assert all(row.reason for row in rows)
    assert rows[0].status == "available"
    assert rows[0].harness is agents.Harness.MEIVA
    assert rows[0].models == ()


def test_fork_without_a_machine_names_none_and_leaves_the_default_to_the_gateway(
    gateway_requests: list[httpx.Request],
) -> None:
    assert agents.spawn(fork_from=7) == 42
    assert agents.spawn(fork_from=7, machine="ubuntu") == 42
    assert agents.spawn() == 42
    bodies = [_body(request) for request in gateway_requests]
    assert [request.method for request in gateway_requests] == ["POST", "POST", "POST"]
    assert "machine" not in bodies[0]
    assert bodies[1]["machine"] == "ubuntu"
    assert bodies[2]["machine"]
