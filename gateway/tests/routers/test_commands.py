from collections.abc import Callable
from typing import NoReturn
from unittest.mock import MagicMock

import pytest

from base.packages.composer_commands import Command, CommandCatalog
from gateway.tests.auth.gateway_config import gateway_test_client
from ops.cluster import rpc as cluster_rpc


def _one_command(
    name: str, description: str, hint: str, body: str
) -> Callable[[CommandCatalog], list[Command]]:

    def discover(_catalog: CommandCatalog) -> list[Command]:
        return [
            {
                "name": name,
                "description": description,
                "instruction_hint": hint,
                "body": body,
                "skill_target": None,
            }
        ]

    return discover


def _agent_pool(machine: str | None) -> MagicMock:
    pool = MagicMock()
    cursor = pool.connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (machine,) if machine else None
    return pool


def _runner_unreachable() -> cluster_rpc.ClusterOpUnreachable:
    return cluster_rpc.ClusterOpUnreachable("runner down")


def _runner_failed() -> cluster_rpc.ClusterOpFailed:
    return cluster_rpc.ClusterOpFailed({"error": "unknown op"})


def test_endpoint_returns_command_metadata():
    with gateway_test_client() as client:
        client.app.state.command_catalog_discoverer = _one_command("recap", "d", "h", "b")
        resp = client.get("/api/commands")
    assert resp.status_code == 200, resp.text
    assert resp.json() == [{"name": "recap", "description": "d", "instruction_hint": "h"}]


def test_endpoint_agent_view_dispatches_to_agents_machine():
    seen: dict[str, object] = {}

    async def _dispatch(
        _db: object, machine: str, kind: str, payload: dict[str, int], *, timeout_s: float
    ) -> dict[str, object]:
        seen["machine"] = machine
        seen["kind"] = kind
        seen["payload"] = payload
        seen["timeout_s"] = timeout_s
        return {
            "commands": [{"name": "project", "description": "d", "instruction_hint": "h"}],
            "mcp_names": ["runner-only-groundwork"],
        }

    with gateway_test_client() as client:
        client.app.state.command_rpc_dispatcher = _dispatch
        client.app.state.db_pool = _agent_pool("runner-a")
        resp = client.get("/api/commands?agent_id=42")
    assert resp.status_code == 200, resp.text
    assert resp.json() == [{"name": "project", "description": "d", "instruction_hint": "h"}]
    assert seen == {
        "machine": "runner-a",
        "kind": "agent_skill_view",
        "payload": {"agent_id": 42},
        "timeout_s": 3.0,
    }


@pytest.mark.parametrize(
    "failure", [_runner_unreachable, _runner_failed], ids=["unreachable", "failed"]
)
def test_endpoint_agent_view_unavailable_falls_back_locally(
    failure: Callable[[], Exception],
):
    async def _unavailable(*_args: object, **_kwargs: object) -> NoReturn:
        raise failure()

    with gateway_test_client() as client:
        client.app.state.command_catalog_discoverer = _one_command("local", "d", "h", "b")
        client.app.state.command_rpc_dispatcher = _unavailable
        client.app.state.db_pool = _agent_pool("runner-a")
        resp = client.get("/api/commands?agent_id=42")
    assert resp.status_code == 200, resp.text
    assert resp.json() == [{"name": "local", "description": "d", "instruction_hint": "h"}]


def test_endpoint_agent_view_missing_agent_falls_back_locally():
    with gateway_test_client() as client:
        client.app.state.command_catalog_discoverer = _one_command("local", "d", "h", "b")
        client.app.state.db_pool = _agent_pool(None)
        resp = client.get("/api/commands?agent_id=999")
    assert resp.status_code == 200, resp.text
    assert resp.json() == [{"name": "local", "description": "d", "instruction_hint": "h"}]
