# meiva:link ops/docs/ops-module.md


from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

import base.packages.mcp_config as mcp_cfg_mod
from base.cluster.machine import MachineIdentity
from base.host.proc import run_bounded
from base.host.system.tests.fake_commands import fake_host
from base.packages.plugins import enable_config, mcp_enabled
from ops.inventory import inventory_read_op, inventory_write_op
from ops.rpc_schemas import FieldWriteResult, InventoryReadResult, InventoryWriteOpResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_machine_mcp(unit_home: Path, servers: Mapping[str, Mapping[str, object]]) -> None:
    (unit_home / "mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


@pytest.fixture
def runner_home(unit_home: Path, machine_identity: MachineIdentity) -> Path:
    machine_identity.set(role=frozenset({"agent-runner"}))
    return unit_home


# ---------------------------------------------------------------------------
# inventory_read_op
# ---------------------------------------------------------------------------


def test_inventory_read_op_lists_real_plugins(
    runner_home: Path, *, machine_identity: MachineIdentity
) -> None:
    """The repo's real built-in plugins (meiva_code, etc.) appear, each with
    an enabled bool + a description. (meiva_compact moved to core, Issue #1284.)"""
    machine_identity.set(name="test-machine")
    result = inventory_read_op(local_machine=machine_identity, host=fake_host())
    assert result.machine == "test-machine"
    plugins = result.plugins
    # Real repo plugins discovered under <repo>/meiva_builtins/plugins/.
    assert "meiva_code" in plugins
    for entry in plugins.values():
        assert isinstance(entry.enabled, bool)
        assert isinstance(entry.description, str)
        # Plugins have no static host gate.
        assert entry.can_enable is None
        assert entry.reason is None


def test_inventory_read_op_mcp_server_default_on(
    runner_home: Path, *, machine_identity: MachineIdentity
) -> None:
    """A machine-config server with no overlay entry shows enabled=True and a
    one-line description."""
    machine_identity.set(name="test-machine")
    _write_machine_mcp(runner_home, {"fs": {"command": "run-fs", "args": ["--root", "/"]}})
    result = inventory_read_op(local_machine=machine_identity, host=fake_host())
    servers = result.mcp_servers
    assert "fs" in servers
    assert "chrome" in servers
    assert servers["fs"].enabled is True
    assert servers["fs"].can_enable is True
    assert servers["fs"].reason is None
    assert servers["fs"].description == "run-fs --root /"


def test_inventory_read_op_overlay_reflected(
    runner_home: Path, *, machine_identity: MachineIdentity
) -> None:
    """A server the per-host overlay marks disabled reads back enabled=False."""
    machine_identity.set(name="test-machine")
    _write_machine_mcp(runner_home, {"X": {"command": "x"}})
    mcp_enabled.set_mcp_enabled("X", enabled=False)
    result = inventory_read_op(local_machine=machine_identity, host=fake_host())
    assert result.mcp_servers["X"].enabled is False


@pytest.mark.parametrize("requires", [[], {"gpu": False}, {"display": "false"}])
def test_inventory_rejects_invalid_requirements_before_read_or_write(
    runner_home: Path,
    requires: object,
    *,
    machine_identity: MachineIdentity,
) -> None:
    machine_identity.set(name="test-machine")
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


def test_inventory_can_disable_server_with_invalid_requirements(
    runner_home: Path, *, machine_identity: MachineIdentity
) -> None:
    machine_identity.set(name="test-machine")
    spec = {"command": "x", "requires": {"display": "false"}}
    _write_machine_mcp(runner_home, {"broken": spec})

    result = inventory_write_op(
        plugins={}, mcp_servers={"broken": False}, local_machine=machine_identity, host=fake_host()
    )
    assert result.applied is True
    assert result.mcp_results["broken"] == FieldWriteResult(ok=True, reason=None)
    assert mcp_enabled.read_enabled() == {"broken": False}
    assert mcp_cfg_mod.read_servers(runner_home / "mcp.json") == {"broken": spec}


# ---------------------------------------------------------------------------
# inventory_write_op
# ---------------------------------------------------------------------------


def test_inventory_write_op_plugin_happy_path(
    runner_home: Path, *, machine_identity: MachineIdentity
) -> None:
    """Disabling a present plugin -> applied=True, and the local config reflects it."""
    machine_identity.set(name="test-machine")
    result = inventory_write_op(
        plugins={"meiva_code": False},
        mcp_servers={},
        local_machine=machine_identity,
        host=fake_host(),
    )
    assert result.applied is True
    assert result.plugin_results["meiva_code"] == FieldWriteResult(ok=True, reason=None)
    # The per-machine plugins_config.json now records meiva_code as disabled.
    cfg = enable_config.load(set(enable_config.discover_plugins()))
    assert cfg.plugins["meiva_code"].enabled is False


def test_inventory_write_op_atomic_rejects_unknown_plugin(
    runner_home: Path, *, machine_identity: MachineIdentity
) -> None:
    """A payload mixing a present plugin toggle + an unknown plugin is rejected
    atomically: applied=False, the present plugin's file is UNCHANGED."""
    machine_identity.set(name="test-machine")
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
    # Atomicity: nothing was written, so the local file does not exist yet.
    assert not enable_config.local_config_path().exists()


def test_inventory_write_op_rejects_unknown_mcp_server(
    runner_home: Path, *, machine_identity: MachineIdentity
) -> None:
    """An MCP name not in the merged map is rejected with a reason; applied=False."""
    machine_identity.set(name="test-machine")
    result = inventory_write_op(
        plugins={}, mcp_servers={"ghost": True}, local_machine=machine_identity, host=fake_host()
    )
    assert result.applied is False
    assert result.mcp_results["ghost"].ok is False
    assert "known MCP server" in (result.mcp_results["ghost"].reason or "")


def test_inventory_write_op_mcp_capability_rejected(
    runner_home: Path, *, machine_identity: MachineIdentity
) -> None:
    """A server requiring a display, on a host with none, can't be enabled
    (applied=False) but can always be disabled."""
    machine_identity.set(name="test-machine")
    _write_machine_mcp(runner_home, {"that": {"command": "x", "requires": {"display": True}}})

    child = run_bounded(
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("_host_capability_child.py")),
            "inventory",
        ],
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


def test_inventory_write_op_empty_payload_trivially_applied(
    runner_home: Path, *, machine_identity: MachineIdentity
) -> None:
    """Empty payload -> applied=True, nothing written."""
    machine_identity.set(name="test-machine")
    result = inventory_write_op(
        plugins={}, mcp_servers={}, local_machine=machine_identity, host=fake_host()
    )
    assert result.applied is True
    assert result.plugin_results == {}
    assert result.mcp_results == {}
    assert not enable_config.local_config_path().exists()
    assert mcp_enabled.read_enabled() == {}


def test_inventory_write_op_applies_both_plugin_and_mcp(
    runner_home: Path, *, machine_identity: MachineIdentity
) -> None:
    """A mixed payload (present plugin + known MCP, both passing) writes both."""
    machine_identity.set(name="test-machine")
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


# ---------------------------------------------------------------------------
# agent-runner-only precondition
# ---------------------------------------------------------------------------


def test_inventory_ops_reject_gateway(*, machine_identity: MachineIdentity) -> None:
    """On a gateway both ops raise before touching any local file — the
    invariant that inventory is agent-runner-only, made loud rather than silently
    surfacing the gateway checkout's built-in plugins/MCP."""
    machine_identity.set(role=frozenset({"gateway"}))
    with pytest.raises(RuntimeError, match="only on an agent-runner"):
        inventory_read_op(local_machine=machine_identity, host=fake_host())
    with pytest.raises(RuntimeError, match="only on an agent-runner"):
        inventory_write_op(
            plugins={}, mcp_servers={}, local_machine=machine_identity, host=fake_host()
        )
