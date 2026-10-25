# meiva:note
# The inventory ops against real files: the unit home holds the machine MCP config, the per-host
# overlay and the plugin enable file, and the real built-in plugins are discovered from the
# checkout. The display requirement is tested in a child process whose environment is fixed before
# import. Nothing is patched.

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

import base.packages.mcp_config as mcp_cfg_mod
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.host.proc import run_bounded
from base.host.system.tests.fake_commands import fake_host
from base.packages.plugins import enable_config, mcp_enabled
from base.paths.tests.homes import unit_home as unit_home
from ops.inventory import inventory_read_op, inventory_write_op
from ops.rpc_schemas import FieldWriteResult, InventoryReadResult, InventoryWriteOpResult


def _write_machine_mcp(unit_home: Path, servers: Mapping[str, Mapping[str, object]]) -> None:
    (unit_home / "mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


@pytest.fixture
def runner_home(unit_home: Path, machine_identity: ConfiguredMachineIdentity) -> Path:
    machine_identity.set(name="test-machine", role=frozenset({"agent-runner"}))
    return unit_home


# meiva:note
# The repo's real built-in plugins (meiva_code and the others) appear, each with an enabled flag
# and a description, and none has a static host gate.
def test_inventory_read_op_lists_real_plugins(
    runner_home: Path, machine_identity: MachineIdentityReader
) -> None:
    result = inventory_read_op(local_machine=machine_identity, host=fake_host())

    assert result.machine == "test-machine"
    assert "meiva_code" in result.plugins
    for entry in result.plugins.values():
        assert isinstance(entry.enabled, bool)
        assert isinstance(entry.description, str)
        assert entry.can_enable is None
        assert entry.reason is None


# meiva:note
# A machine-config server with no overlay entry shows enabled and a one-line description.
def test_inventory_read_op_mcp_server_default_on(
    runner_home: Path, machine_identity: MachineIdentityReader
) -> None:
    _write_machine_mcp(runner_home, {"fs": {"command": "run-fs", "args": ["--root", "/"]}})

    servers = inventory_read_op(local_machine=machine_identity, host=fake_host()).mcp_servers

    assert "fs" in servers
    assert "chrome" in servers
    assert servers["fs"].enabled is True
    assert servers["fs"].can_enable is True
    assert servers["fs"].reason is None
    assert servers["fs"].description == "run-fs --root /"


# meiva:note
# A server the per-host overlay marks disabled reads back as not enabled.
def test_inventory_read_op_overlay_reflected(
    runner_home: Path, machine_identity: MachineIdentityReader
) -> None:
    _write_machine_mcp(runner_home, {"X": {"command": "x"}})
    mcp_enabled.set_mcp_enabled("X", enabled=False)

    result = inventory_read_op(local_machine=machine_identity, host=fake_host())

    assert result.mcp_servers["X"].enabled is False


@pytest.mark.parametrize("requires", [[], {"gpu": False}, {"display": "false"}])
def test_inventory_rejects_invalid_requirements_before_read_or_write(
    runner_home: Path, requires: object, machine_identity: MachineIdentityReader
) -> None:
    # meiva:note
    # A malformed `requires` fails the read and the write loudly, and the write leaves both the
    # overlay and the plugin file untouched.
    _write_machine_mcp(runner_home, {"broken": {"command": "x", "requires": requires}})
    mcp_enabled.set_mcp_enabled("broken", enabled=False)
    before = mcp_enabled.local_config_path().read_bytes()

    with pytest.raises(mcp_cfg_mod.MCPError, match="requires"):
        inventory_read_op(local_machine=machine_identity, host=fake_host())
    with pytest.raises(mcp_cfg_mod.MCPError, match="requires"):
        inventory_write_op(
            plugins={"meiva_code": False},
            mcp_servers={"broken": True},
            local_machine=machine_identity,
            host=fake_host(),
        )

    assert mcp_enabled.local_config_path().read_bytes() == before
    assert not enable_config.local_config_path().exists()


# meiva:note
# Disabling a server whose requirements are malformed is always allowed: nothing needs judging.
def test_inventory_can_disable_server_with_invalid_requirements(
    runner_home: Path, machine_identity: MachineIdentityReader
) -> None:
    spec = {"command": "x", "requires": {"display": "false"}}
    _write_machine_mcp(runner_home, {"broken": spec})

    result = inventory_write_op(
        plugins={}, mcp_servers={"broken": False}, local_machine=machine_identity, host=fake_host()
    )

    assert result.applied is True
    assert result.mcp_results["broken"] == FieldWriteResult(ok=True, reason=None)
    assert mcp_enabled.read_enabled() == {"broken": False}
    assert mcp_cfg_mod.read_servers(runner_home / "mcp.json") == {"broken": spec}


# meiva:note
# Disabling a present plugin applies, and the local plugin config reflects it.
def test_inventory_write_op_plugin_happy_path(
    runner_home: Path, machine_identity: MachineIdentityReader
) -> None:
    result = inventory_write_op(
        plugins={"meiva_code": False},
        mcp_servers={},
        local_machine=machine_identity,
        host=fake_host(),
    )

    assert result.applied is True
    assert result.plugin_results["meiva_code"] == FieldWriteResult(ok=True, reason=None)
    cfg = enable_config.load(set(enable_config.discover_plugins()))
    assert cfg.plugins["meiva_code"].enabled is False


# meiva:note
# A payload mixing a present plugin toggle with an unknown plugin is rejected atomically:
# nothing is applied and the plugin file is not even created.
def test_inventory_write_op_atomic_rejects_unknown_plugin(
    runner_home: Path, machine_identity: MachineIdentityReader
) -> None:
    result = inventory_write_op(
        plugins={"meiva_code": False, "does-not-exist": True},
        mcp_servers={},
        local_machine=machine_identity,
        host=fake_host(),
    )

    assert result.applied is False
    assert result.plugin_results["meiva_code"].ok is True
    assert result.plugin_results["does-not-exist"].ok is False
    assert "not installed" in (result.plugin_results["does-not-exist"].reason or "")
    assert not enable_config.local_config_path().exists()


# meiva:note
# An MCP name that is not in the merged map is rejected with a reason.
def test_inventory_write_op_rejects_unknown_mcp_server(
    runner_home: Path, machine_identity: MachineIdentityReader
) -> None:
    result = inventory_write_op(
        plugins={}, mcp_servers={"ghost": True}, local_machine=machine_identity, host=fake_host()
    )

    assert result.applied is False
    assert result.mcp_results["ghost"].ok is False
    assert "known MCP server" in (result.mcp_results["ghost"].reason or "")


# meiva:note
# A server requiring a display, on a host with none, cannot be enabled but can always be disabled.
# The child process has no display in its environment, fixed before application import.
def test_inventory_write_op_mcp_capability_rejected(runner_home: Path) -> None:
    _write_machine_mcp(runner_home, {"that": {"command": "x", "requires": {"display": True}}})

    child = run_bounded(
        [sys.executable, "-I", str(Path(__file__).with_name("_capability_child.py"))],
        env={
            name: value
            for name, value in os.environ.items()
            if name not in {"DISPLAY", "WAYLAND_DISPLAY", "MEIVA_PROCESS_PROFILE"}
        },
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert child.returncode == 0, child.stderr
    result = json.loads(child.stdout)
    inventory = InventoryReadResult.model_validate(result["inventory"])
    assert inventory.mcp_servers["that"].can_enable is False
    enable = InventoryWriteOpResult.model_validate(result["enable"])
    assert enable.applied is False
    assert enable.mcp_results["that"].ok is False
    assert enable.mcp_results["that"].reason == "requires a display, but this host has none"
    assert result["after_enable"] == {}
    disable = InventoryWriteOpResult.model_validate(result["disable"])
    assert disable.applied is True
    assert disable.mcp_results["that"] == FieldWriteResult(ok=True, reason=None)
    assert mcp_enabled.read_enabled() == {"that": False}


# meiva:note
# An empty payload is trivially applied and writes nothing.
def test_inventory_write_op_empty_payload_trivially_applied(
    runner_home: Path, machine_identity: MachineIdentityReader
) -> None:
    result = inventory_write_op(
        plugins={}, mcp_servers={}, local_machine=machine_identity, host=fake_host()
    )

    assert result.applied is True
    assert result.plugin_results == {}
    assert result.mcp_results == {}
    assert not enable_config.local_config_path().exists()
    assert mcp_enabled.read_enabled() == {}


# meiva:note
# A mixed payload (a present plugin and a known MCP server, both passing) writes both.
def test_inventory_write_op_applies_both_plugin_and_mcp(
    runner_home: Path, machine_identity: MachineIdentityReader
) -> None:
    _write_machine_mcp(runner_home, {"fs": {"command": "x"}})

    result = inventory_write_op(
        plugins={"meiva_code": False},
        mcp_servers={"fs": False},
        local_machine=machine_identity,
        host=fake_host(),
    )

    assert result.applied is True
    cfg = enable_config.load(set(enable_config.discover_plugins()))
    assert cfg.plugins["meiva_code"].enabled is False
    assert mcp_enabled.read_enabled() == {"fs": False}


# meiva:note
# On a gateway both ops raise before touching any local file: inventory is agent-runner-only, made
# loud rather than silently surfacing the gateway checkout's built-in plugins and MCP servers.
def test_inventory_ops_reject_gateway(machine_identity: ConfiguredMachineIdentity) -> None:
    machine_identity.set(role=frozenset({"gateway"}))

    with pytest.raises(RuntimeError, match="only on an agent-runner"):
        inventory_read_op(local_machine=machine_identity, host=fake_host())
    with pytest.raises(RuntimeError, match="only on an agent-runner"):
        inventory_write_op(
            plugins={}, mcp_servers={}, local_machine=machine_identity, host=fake_host()
        )
