from __future__ import annotations

import json
from pathlib import Path

import meiva.mcps as mcps_mod
from base.agents.context import MeivaContext
from base.clock.tests.fakes import system_clock
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot
from base.config.tests.configuration import snapshot_process_config
from meiva.boot import derived_clients
from meiva.mcps import McpClients
from meiva.sdk_surface import process_context


# meiva:note
# A context over a configuration owner of its own, so a timeout set on one owner never reaches another context.
def context_with_timeout(timeout: float) -> tuple[MeivaContext, ConfigBoot]:
    config = snapshot_process_config()
    config.set_field("mcp_connect_timeout_seconds", timeout)
    clients = derived_clients(config, ConfiguredMachineIdentity(config))
    return MeivaContext(clients=clients, clock_factory=system_clock), config


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
# Each context owns its own MCP clients, which read the timeout of their own configuration owner once, when the first MCP call builds them. The facade hands that value to the source of every proxy, so a timeout changed afterwards reaches only the clients built after it: a closed context rebuilds its clients on the next use and reads the setting again.
def test_sdk_clients_read_their_own_timeout_once_and_rebuild_after_close(
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
        assert (sdk_timeout(first), sdk_timeout(second)) == (7.5, 11.0)

        first.clients.close()
        rebuilt = first.clients.get(McpClients)
        assert rebuilt is not first_client
        assert rebuilt.timeout_seconds == 2.0
        assert second_client.timeout_seconds == 11.0
        assert rebuilt._remote is None
    finally:
        first.clients.close()
        second.clients.close()


# meiva:note
# Building the client set builds no MCP client and reads no setting: the timeout is read when the first MCP call asks for the clients, so a value set between the two is the one the clients hold.
def test_mcp_client_construction_waits_for_the_first_call_to_read_configuration() -> None:
    config = snapshot_process_config()
    clients = derived_clients(config, ConfiguredMachineIdentity(config))
    assert not clients._made

    config.set_field("mcp_connect_timeout_seconds", 3.5)
    mcp = clients.get(McpClients)

    assert mcp.timeout_seconds == 3.5
    assert mcp._remote is None
    clients.close()
