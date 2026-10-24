from __future__ import annotations

import json
from pathlib import Path

import meiva.mcps as mcps_mod
from base.agents.context import MeivaContext
from base.config import ConfigBoot
from base.config.tests.configuration import snapshot_process_config
from meiva.boot import process_clients
from meiva.mcps import McpClients
from meiva.sdk_surface import process_context


# meiva:note
# A context over a configuration owner of its own, so a timeout set on one owner never reaches another context.
def context_with_timeout(timeout: float) -> tuple[MeivaContext, ConfigBoot]:
    config = snapshot_process_config()
    config.set_field("mcp_connect_timeout_seconds", timeout)
    return MeivaContext(clients=process_clients(config=config)), config


def sdk_timeout(context: MeivaContext) -> float:
    previous = process_context.unbind_context()
    try:
        process_context.bind_context(context)
        return mcps_mod.fs._source.timeout_seconds
    finally:
        process_context.unbind_context()
        if previous is not None:
            process_context.bind_context(previous)


# meiva:note
# Each context owns its own MCP clients and reads the live setting of its own configuration owner. The facade reads that setting when it builds the source of a proxy, so a changed timeout reaches the next proxy of that context only, and a closed context rebuilds its clients on the next use.
def test_sdk_clients_use_separate_live_timeout_readers_and_rebuild_after_close(
    unit_home: Path,
) -> None:
    (unit_home / "mcp.json").write_text(json.dumps({"mcpServers": {"fs": {"command": "x"}}}))
    first, first_config = context_with_timeout(7.5)
    second, _ = context_with_timeout(11.0)
    try:
        first_client = first.clients.get(McpClients)
        second_client = second.clients.get(McpClients)
        assert first_client is not second_client
        assert first_client._remote is None and second_client._remote is None
        assert (sdk_timeout(first), sdk_timeout(second)) == (7.5, 11.0)

        first_config.set_field("mcp_connect_timeout_seconds", 2.0)
        assert (sdk_timeout(first), sdk_timeout(second)) == (2.0, 11.0)

        first.clients.close()
        rebuilt = first.clients.get(McpClients)
        assert rebuilt is not first_client
        assert rebuilt.timeout_seconds() == 2.0
        assert second_client.timeout_seconds() == 11.0
        assert rebuilt._remote is None
    finally:
        first.clients.close()
        second.clients.close()


def test_mcp_client_construction_does_not_read_configuration() -> None:
    def premature_read() -> float:
        raise AssertionError("configuration belongs to an operation or session creation")

    assert McpClients(Path("unused.sock"), premature_read)._remote is None

    clients = process_clients(config=snapshot_process_config())
    assert not clients._made
    mcp = clients.get(McpClients)
    assert mcp._remote is None
    clients.close()
