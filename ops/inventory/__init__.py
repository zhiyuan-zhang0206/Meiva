# meiva:note
# Plugin + MCP-enable inventory RPC ops (agent-runner only).
#
# Read this host's plugin / MCP enable inventory (each with metadata and a host capability
# verdict) and apply a validated all-or-nothing toggle write. One of the op clusters beside
# `ops.lifecycle`, `ops.cluster.operations` and `ops.host_config`; each cluster is
# self-contained. Plugins and MCP servers are an agent-runner-only concern, so every op asserts
# the agent-runner capability and fails loudly on a gateway. Dispatched by the agent-runner ops
# server (`services/agent_runner/agent_ops/daemon.py`). The shell reads what this host has and
# writes the toggles; `toggles` decides which toggles may be written.

from __future__ import annotations

from base.cluster.machine import MachineIdentity
from base.host.system.hostport import Host
from base.packages.mcp_config import load_mcp_config, server_capability
from base.packages.plugins import enable_config, mcp_enabled
from ops.inventory.toggles import (
    Verdict,
    mcp_summary,
    plan_toggles,
    require_agent_runner,
    servers_to_probe,
)
from ops.rpc_schemas import (
    FieldWriteResult,
    InventoryReadItem,
    InventoryReadResult,
    InventoryWriteOpResult,
)

__all__ = ["inventory_read_op", "inventory_write_op"]


# meiva:note
# Read this host's plugin + MCP enable inventory, each with metadata.
#
# Returns an InventoryReadResult with:
#   - machine: this machine's name
#   - plugins: one entry per discovered plugin with effective enabled flag, a description, and
#     no static host gate (can_enable/reason are None; an installed plugin is always enable-able)
#   - mcp_servers: one entry per server in the full merged map with effective enabled flag (the
#     per-host overlay default-on), a one-line description, and a read-time capability verdict
#     (can_enable/reason) for the `requires` precondition
def inventory_read_op(*, local_machine: MachineIdentity, host: Host) -> InventoryReadResult:
    require_agent_runner(local_machine.role())
    discovered = enable_config.discover_plugins()
    cfg = enable_config.load_for_runtime(set(discovered))
    plugins: dict[str, InventoryReadItem] = {}
    for name, plugin_dir in discovered.items():
        try:
            description = enable_config.parse_description(plugin_dir / "plugin.py")
        except (OSError, SyntaxError):
            description = ""
        plugins[name] = InventoryReadItem(
            enabled=cfg.plugins[name].enabled,
            can_enable=None,
            reason=None,
            description=description,
        )

    merged = load_mcp_config(include_disabled=True)
    overlay = mcp_enabled.read_enabled()
    mcp_servers: dict[str, InventoryReadItem] = {}
    for name, spec in merged.items():
        ok, reason = server_capability(spec, host=host)
        mcp_servers[name] = InventoryReadItem(
            enabled=overlay.get(name, True),
            can_enable=ok,
            reason=reason,
            description=mcp_summary(spec),
        )

    return InventoryReadResult(
        machine=local_machine.name(),
        plugins=plugins,
        mcp_servers=mcp_servers,
    )


# meiva:note
# Validate and atomically apply a set of plugin + MCP enable toggles.
#
# Validates every requested toggle first (no writes), mirroring `config_write_op`'s
# all-or-nothing atomicity:
#   - a plugin not installed on this host is rejected; enabling and disabling an installed
#     plugin are both always ok
#   - an MCP name not in the merged map is rejected; enabling one runs the host capability check
#     (`server_capability`); disabling is always ok
#
# Only writes if every requested toggle passes. Returns per-item results and an `applied` flag.
# No restart_required field: plugin/MCP toggles take effect on the next agent step / next
# connect, no restart.
def inventory_write_op(
    plugins: dict[str, bool],
    mcp_servers: dict[str, bool],
    *,
    local_machine: MachineIdentity,
    host: Host,
) -> InventoryWriteOpResult:
    require_agent_runner(local_machine.role())
    merged = load_mcp_config(include_disabled=True)
    capabilities = {
        name: Verdict(*server_capability(merged[name], host=host))
        for name in servers_to_probe(mcp_servers, frozenset(merged))
    }
    verdicts = plan_toggles(
        plugins,
        mcp_servers,
        installed_plugins=frozenset(enable_config.discover_plugins()),
        known_servers=frozenset(merged),
        capabilities=capabilities,
    )

    if verdicts.applied:
        for name, want in plugins.items():
            enable_config.set_local_enabled(name, enabled=want)
        for name, want in mcp_servers.items():
            mcp_enabled.set_mcp_enabled(name, enabled=want)

    return InventoryWriteOpResult(
        machine=local_machine.name(),
        plugin_results={
            name: FieldWriteResult(ok=verdict.ok, reason=verdict.reason)
            for name, verdict in verdicts.plugins
        },
        mcp_results={
            name: FieldWriteResult(ok=verdict.ok, reason=verdict.reason)
            for name, verdict in verdicts.mcp_servers
        },
        applied=verdicts.applied,
    )
