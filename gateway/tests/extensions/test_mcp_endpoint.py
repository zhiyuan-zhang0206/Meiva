from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any, cast

import psycopg
import pytest

from base import config
from base.host.net.auth import bearer_header
from gateway.tests.mcp_calls import enable_endpoint as enable_endpoint
from gateway.tests.mcp_calls import (
    initialize,
    post_mcp,
    post_message,
    tool_call,
    tool_result,
)
from tests.fixtures.gateway_config import GatewayTestClient, gateway_test_client

_SECRET = "test-cluster-secret"  # noqa: S105 — test fixture
_TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


def _error_envelope(result: dict[str, Any]) -> dict[str, Any]:
    assert result["result"].get("isError") is True
    text = tool_result(result)
    return json.loads(text[text.index("{") :])


def _stable(value: Any) -> Any:
    if isinstance(value, dict):
        fields = cast(dict[str, Any], value)
        return {k: _stable(v) for k, v in fields.items() if k != "observed_at"}
    if isinstance(value, list):
        return [_stable(v) for v in cast(list[Any], value)]
    return value


def _spawn(client: GatewayTestClient, key: str = "birth-intent") -> int:
    spawned = tool_result(
        tool_call(client, "spawn_agent_guarded_v1", {"idempotency_key": key, "prompt": "goal"})
    )
    return int(spawned["id"])


def test_disabled_endpoint_answers_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.settings.gateway, "mcp_endpoint_enabled", False)
    with gateway_test_client() as client:
        resp = post_mcp(client, _TOOLS_LIST)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "mcp endpoint is disabled"
    assert resp.json()["code"] == "mcp_endpoint_disabled"


def test_initialize_negotiates_and_lists_the_tool_surface() -> None:
    with gateway_test_client() as client:
        init = initialize(client)
        assert init[0]["result"]["protocolVersion"]
        messages = post_message(client, _TOOLS_LIST)
    tools = {t["name"]: t for t in messages[0]["result"]["tools"]}
    assert set(tools) == {
        "list_agents",
        "get_agent",
        "spawn_agent_guarded_v1",
        "send_message",
        "get_messages",
        "terminate_agent",
        "cluster_status",
    }
    assert set(tools["spawn_agent_guarded_v1"]["inputSchema"]["required"]) == {
        "prompt",
        "idempotency_key",
    }
    terminate_description = " ".join(tools["terminate_agent"]["description"].split())
    assert "requests interruption" in terminate_description
    assert "Use force only when a clean stop cannot progress" in terminate_description


@pytest.mark.parametrize("authorization", [None, "Bearer not-the-cluster-secret"])
def test_required_mode_refuses_missing_or_bad_credentials(
    monkeypatch: pytest.MonkeyPatch, authorization: str | None
) -> None:
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", _SECRET)
    headers = {} if authorization is None else {"Authorization": authorization}
    with gateway_test_client() as client:
        client.headers.pop("Authorization")
        resp = post_mcp(client, _TOOLS_LIST, headers)
    assert resp.status_code == 401
    assert resp.json()["code"] == "authentication_required"


def test_the_cluster_bearer_calls_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", _SECRET)
    with gateway_test_client() as client:
        client.headers.pop("Authorization")
        listed = tool_call(client, "list_agents", {}, headers=bearer_header(_SECRET))
        anonymous = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "list_agents", "arguments": {}},
            },
        )
    assert tool_result(listed) == {"agents": [], "next_cursor": None}
    assert anonymous.status_code == 401


@contextmanager
def _trusted_network_client(base: GatewayTestClient) -> Generator[GatewayTestClient]:
    cfg = base.app.state.gateway_config
    with gateway_test_client(
        process=base.app.state.gateway_process,
        config=replace(
            cfg,
            human_auth="trusted_network",
            trusted_hosts=frozenset({*cfg.trusted_hosts, "testserver"}),
        ),
    ) as client:
        client.headers.pop("Authorization")
        yield client


def test_trusted_network_serves_an_anonymous_call(db_conn: psycopg.Connection) -> None:
    with gateway_test_client() as base, _trusted_network_client(base) as client:
        agent_id = _spawn(client)
        row = tool_result(tool_call(client, "get_agent", {"agent_id": agent_id}))
    assert row["agent_id"] == agent_id
    assert db_conn.execute("SELECT count(*) FROM agents").fetchone() == (1,)


def test_trusted_network_still_refuses_an_unknown_host() -> None:
    with gateway_test_client() as base, _trusted_network_client(base) as client:
        resp = post_mcp(client, _TOOLS_LIST, {"Host": "rebind.attacker.example"})
    assert resp.status_code == 403


def test_tool_results_equal_the_direct_api_responses() -> None:
    with gateway_test_client() as client:
        agent_id = _spawn(client)
        listed = tool_result(tool_call(client, "list_agents", {}))
        assert _stable(listed) == _stable(client.get("/api/agents").json())
        assert [card["agent_id"] for card in listed["agents"]] == [agent_id]
        row = tool_result(tool_call(client, "get_agent", {"agent_id": agent_id}))
        assert _stable(row) == _stable(client.get(f"/api/agents/{agent_id}").json())
        assert row["spawner"] == "mcp"
        messages = tool_result(tool_call(client, "get_messages", {"agent_id": agent_id}))
        assert messages == client.get(f"/api/agents/{agent_id}/messages").json()


def test_list_agents_passes_its_arguments_as_query_parameters() -> None:
    with gateway_test_client() as client:
        _spawn(client)
        args = {"scope": "terminated", "query": "x", "before_id": 5, "limit": 3}
        result = tool_result(tool_call(client, "list_agents", args))
        assert _stable(result) == _stable(client.get("/api/agents", params=args).json())
        assert result["agents"] == []


def test_cluster_status_reports_this_host() -> None:
    with gateway_test_client() as client:
        client.app.state.machine.set(role="gateway")
        payload = tool_result(tool_call(client, "cluster_status", {}))
        assert _stable(payload) == _stable(client.get("/api/cluster/status").json())
    assert payload["machine_name"]


def test_spawn_message_terminate_round_trip(db_conn: psycopg.Connection) -> None:
    with gateway_test_client() as client:
        agent_id = _spawn(client)
        sent = tool_result(
            tool_call(client, "send_message", {"agent_id": agent_id, "content": "hi"})
        )
        assert sent["status"] in {"queued", "claimed", "idling"}
        stopped = tool_result(
            tool_call(
                client,
                "terminate_agent",
                {"agent_id": agent_id, "message": "retain this result"},
            )
        )
        assert stopped["status"] == "enqueued"
    assert db_conn.execute(
        "SELECT content,kind,source FROM inbound_messages WHERE agent_id=%s ORDER BY id",
        (agent_id,),
    ).fetchall() == [
        ("goal", "chat", "user"),
        ("hi", "chat", "user"),
        ("retain this result", "chat", "user"),
        ("", "terminate", "user"),
    ]


def test_send_message_passes_the_declared_source_to_the_api(
    db_conn: psycopg.Connection,
) -> None:
    with gateway_test_client() as client:
        agent_id = _spawn(client)
        tool_call(client, "send_message", {"agent_id": agent_id, "content": "as the user"})
        peer = {"agent_id": agent_id, "content": "as a peer", "source": "external_agent:codex:r1"}
        assert not tool_call(client, "send_message", peer)["result"].get("isError")
        bad = tool_call(
            client, "send_message", {"agent_id": agent_id, "content": "x", "source": "bogus"}
        )
        direct = client.post(
            f"/api/agents/{agent_id}/messages", json={"content": "x", "source": "bogus"}
        )
        envelope = _error_envelope(bad)
        assert (envelope["status"], envelope["code"]) == (422, direct.json()["code"])
    rows = dict(
        db_conn.execute(
            "SELECT content, source FROM inbound_messages WHERE agent_id = %s", (agent_id,)
        ).fetchall()
    )
    assert rows["as the user"] == "user"
    assert rows["as a peer"] == "external_agent:codex:r1"
    assert "x" not in rows


def test_spawn_passes_prompt_source_and_spawner_to_the_api(db_conn: psycopg.Connection) -> None:
    with gateway_test_client() as client:
        spawned = tool_result(
            tool_call(
                client,
                "spawn_agent_guarded_v1",
                {
                    "idempotency_key": "declared",
                    "prompt": "goal",
                    "prompt_source": "external_agent:codex",
                    "spawner": "codex",
                },
            )
        )
        bad = tool_call(
            client,
            "spawn_agent_guarded_v1",
            {"idempotency_key": "bad", "prompt": "goal", "prompt_source": "bogus"},
        )
    assert _error_envelope(bad)["status"] == 422
    assert db_conn.execute(
        "SELECT source FROM inbound_messages WHERE agent_id = %s", (spawned["id"],)
    ).fetchall() == [("external_agent:codex",)]


def test_idempotency_keys_reach_the_api() -> None:
    with gateway_test_client() as client:
        first = _spawn(client)
        assert _spawn(client) == first
        args = {"agent_id": first, "content": "same", "idempotency_key": "same-key"}
        one = tool_result(tool_call(client, "send_message", args))
        repeat = tool_result(tool_call(client, "send_message", args))
        assert one["inbound_id"] == repeat["inbound_id"]
        conflict = tool_call(client, "send_message", args | {"content": "different"})
        assert _error_envelope(conflict)["status"] == 409
        changed = tool_call(
            client, "spawn_agent_guarded_v1", {"idempotency_key": "birth-intent", "prompt": "new"}
        )
        assert _error_envelope(changed)["status"] == 409


def test_an_api_error_is_a_tool_error_with_the_api_envelope() -> None:
    with gateway_test_client() as client:
        result = tool_call(client, "get_agent", {"agent_id": 999999})
        direct = client.get("/api/agents/999999")
    envelope = _error_envelope(result)
    assert direct.status_code == 404
    assert (envelope["code"], envelope["status"], envelope["detail"]) == (
        direct.json()["code"],
        404,
        direct.json()["detail"],
    )


@pytest.mark.parametrize(
    "arguments",
    [{"limit": 0}, {"limit": 201}, {"before_id": 0}, {"query": "x" * 201}],
)
def test_the_api_rejects_invalid_directory_arguments(arguments: dict[str, object]) -> None:
    with gateway_test_client() as client:
        result = tool_call(client, "list_agents", arguments)
    assert _error_envelope(result)["status"] == 422


def test_spawn_launch_failure_tool_error_names_committed_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base.agents.observation.evidence import AvailabilityReason
    from gateway.agents import creation
    from gateway.agents.forward import LaunchForwardError
    from ops.rpc_schemas import LaunchAgentRequest, SpawnedAgent

    async def _fail(_db: object, _target: str, _body: LaunchAgentRequest) -> SpawnedAgent:
        raise LaunchForwardError(AvailabilityReason.LAUNCH_UNREACHABLE, "runner offline")

    monkeypatch.setattr(creation, "forward_spawn_to_remote", _fail)
    with gateway_test_client() as client:
        response = tool_call(
            client,
            "spawn_agent_guarded_v1",
            {"idempotency_key": "birth-intent", "prompt": "Keep this task"},
        )
    assert response["result"]["isError"] is True
    message = tool_result(response)
    assert "agent_id" in message
    assert "retry_launch_path" in message
    assert "launch_unreachable" in message
