"""Plugin-registered ops services — the discovery + folding machinery in services.supervision.roster.plugin_services.

A plugin ships a `services.py` exposing `services() -> tuple[ServiceSpec, ...]`;
`services.supervision.roster.plugin_services.plugin_services()` discovers the INSTALLED plugins (by code presence,
via `base.packages.plugins.enable_config`) and folds their specs onto `build_services()` so the
roster stays single-source. These lock the load-bearing invariants:
- the real meiva_fleet plugin registers task-maintenance (venv-direct cmd whose
  module lives in the plugin namespace, not core `services.*`, as a standard
  `/healthz` daemon);
- discovery keys on presence, NOT the agent-facing enable-state (a plugin
  disabled via plugins_config still contributes its service);
- no installed plugins -> nothing folded;
- a session-name collision fails fast (the roster is keyed on `session`);
- a present broken / declaration-less `services.py` reports and aborts the roster;
  an absent optional services face contributes nothing.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import base.packages.plugins.enable_config as pc
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.host.system.tests.fake_commands import fake_host
from base.log.tests.log_records import LogRecords
from base.paths import meiva_home
from services.supervision.roster import composition, service_spec, spec
from services.supervision.roster import plugin_services as roster_plugins


def test_fleet_plugin_registers_task_maintenance() -> None:
    """task-maintenance is contributed by the meiva_fleet plugin, not hardcoded in
    ops — its entry module lives under the plugin namespace."""
    by_session = {s.session: s for s in composition.build_services(home=meiva_home())}
    tm = by_session["task-maintenance"]
    # venv-direct launch (no `uv run` wrapper), relative to the source checkout.
    assert (
        tm.cmd == ".venv/bin/python -m meiva_builtins.plugins.meiva_fleet.task_maintenance.daemon"
    )
    assert tm.health_name == "task_maintenance"
    assert tm.capabilities == frozenset({"gateway"})
    # It carries its own gate (the fleet toggle travels with the plugin).
    assert tm.gate is not None


def test_ops_spec_has_no_task_maintenance_hardcoded() -> None:
    """The core roster groups must not name task-maintenance — it only reaches the
    roster via plugin discovery."""
    source = Path(composition.__file__).read_text()
    literals = {
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "task-maintenance" not in literals


def test_no_installed_plugins_contributes_nothing() -> None:
    """When no plugin is present, `plugin_services()` contributes nothing and
    task-maintenance is absent from the roster."""
    names = {s.session for s in composition.build_services(plugin_dirs={}, home=meiva_home())}
    assert "task-maintenance" not in names


def test_discovery_ignores_agent_enable_state() -> None:
    """Roster discovery keys on plugin PRESENCE, not the agent-facing enable-state:
    even with every plugin marked disabled in plugins_config, task-maintenance is
    still in the roster — the machine roster must not depend on the
    agent-plugin-registration plane. Its host-owned on/off is the explicit
    Fleet config image gate, exercised in
    `test_plugin_gate_flows_through_annotation`."""
    config_path = pc.local_config_path(home=meiva_home())
    known = set(pc.installed_plugin_dirs(home=meiva_home()))
    pc.write_local({"plugins": {n: {"enabled": False} for n in sorted(known)}}, home=meiva_home())
    try:
        assert not any(
            entry.enabled for entry in pc.load(known, home=meiva_home()).plugins.values()
        )
        names = {s.session for s in composition.build_services(home=meiva_home())}
        assert "task-maintenance" in names
    finally:
        config_path.unlink(missing_ok=True)


def test_session_collision_fails_fast() -> None:
    """A plugin service whose session collides with a core service is rejected —
    the roster is keyed on `session`, a duplicate would silently shadow one."""
    collider = service_spec.ServiceSpec(
        session="gateway",  # collides with the core gateway service
        cmd="noop",
        capabilities=frozenset({"gateway"}),
        requires_db=True,
    )
    with pytest.raises(roster_plugins.PluginServiceError, match="collides"):
        composition.build_services(plugins=(collider,), home=meiva_home())


def test_services_py_without_declare_reports_and_raises(
    tmp_path: Path, loguru_records: LogRecords
) -> None:
    """A present malformed face is not optional absence."""
    plugin_dir = tmp_path / "brokenplugin"
    plugin_dir.mkdir()
    (plugin_dir / "services.py").write_text("X = 1  # no services() function\n")

    with pytest.raises(roster_plugins.PluginServiceError, match="callable services"):
        roster_plugins.plugin_services(plugin_dirs={"brokenplugin": plugin_dir}, home=meiva_home())

    assert any(
        "brokenplugin" in r["message"] and "failed to load" in r["message"] for r in loguru_records
    )


def test_broken_services_py_reports_and_aborts_roster(
    tmp_path: Path, loguru_records: LogRecords
) -> None:
    """An import error cannot produce a successful partial roster."""
    good_dir = tmp_path / "goodplugin"
    good_dir.mkdir()
    (good_dir / "services.py").write_text(
        "from services.supervision.roster.service_spec import ServiceSpec\n"
        "def services(*, home):\n"
        "    return (ServiceSpec(session='probe-good', cmd='noop',\n"
        "            capabilities=frozenset({'gateway'}), requires_db=False),)\n"
    )
    bad_dir = tmp_path / "brokenplugin"
    bad_dir.mkdir()
    (bad_dir / "services.py").write_text("raise RuntimeError('services boom')\n")

    with pytest.raises(RuntimeError, match="services boom"):
        roster_plugins.plugin_services(
            plugin_dirs={"brokenplugin": bad_dir, "goodplugin": good_dir}, home=meiva_home()
        )
    assert any(
        "brokenplugin" in r["message"] and "failed to load" in r["message"] for r in loguru_records
    )


@pytest.mark.parametrize("prior", [False, True])
def test_external_service_healthcheck_loads_without_agent_bootstrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, prior: bool
) -> None:
    """Discovery supplies the exact module identity root health monitoring imports."""
    import importlib
    import sys
    from types import ModuleType

    name = "quota_service_probe"
    dotted = f"plugins.{name}.services"
    if prior:
        monkeypatch.setitem(sys.modules, dotted, ModuleType(dotted))
    else:
        monkeypatch.delitem(sys.modules, dotted, raising=False)
    plugin_dir = tmp_path / name
    plugin_dir.mkdir()
    (plugin_dir / "services.py").write_text(
        "from __future__ import annotations\n"
        "from dataclasses import dataclass\n"
        "from services.supervision.roster.service_spec import ServiceSpec\n"
        "@dataclass\n"
        "class Sample:\n"
        "    value: int = 1\n"
        "def main():\n"
        "    return Sample().value\n"
        "def services(*, home):\n"
        "    return (ServiceSpec(session='quota-probe', cmd='noop',\n"
        "            capabilities=frozenset({'agent-runner'}), requires_db=True,\n"
        "            healthcheck_module=__name__),)\n"
    )
    declared = roster_plugins.plugin_services(plugin_dirs={name: plugin_dir}, home=meiva_home())
    assert len(declared) == 1
    assert declared[0].healthcheck_module == dotted
    assert importlib.import_module(dotted).main() == 1


@pytest.mark.parametrize("prior", [False, True])
def test_failed_service_import_does_not_publish_partial_module(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, prior: bool
) -> None:
    import sys
    from types import ModuleType

    dotted = "plugins.quota_failed_probe.services"
    previous = ModuleType(dotted) if prior else None
    if previous is None:
        monkeypatch.delitem(sys.modules, dotted, raising=False)
    else:
        monkeypatch.setitem(sys.modules, dotted, previous)
    services_py = tmp_path / "services.py"
    services_py.write_text("PARTIAL = True\nraise RuntimeError('broken collector')\n")
    with pytest.raises(RuntimeError, match="broken collector"):
        roster_plugins.load_plugin_module("quota_failed_probe", services_py)
    assert sys.modules.get(dotted) is previous


@pytest.mark.parametrize(
    ("body", "error", "match"),
    [
        ("def services(*, home):\n    return undefined_roster\n", NameError, "undefined_roster"),
        (
            "def services(*, home):\n    return [1]\n",
            roster_plugins.PluginServiceError,
            "tuple\\[ServiceSpec",
        ),
        ("services = 3\n", roster_plugins.PluginServiceError, "callable services"),
        (
            "def services():\n    return ()\n",
            roster_plugins.PluginServiceError,
            "must accept the keyword home",
        ),
        (
            "from pydantic import BaseModel\n"
            "class Config(BaseModel):\n    enabled: bool\n"
            "def services(*, home):\n    Config(enabled='not-a-bool')\n    return ()\n",
            ValueError,
            "validation error",
        ),
    ],
)
def test_invalid_service_face_propagates_to_roster_operation(
    tmp_path: Path,
    loguru_records: LogRecords,
    body: str,
    error: type[Exception],
    match: str,
) -> None:
    plugin_dir = tmp_path / "brokenplugin"
    plugin_dir.mkdir()
    (plugin_dir / "services.py").write_text(body)
    with pytest.raises(error, match=match):
        composition.build_services(plugin_dirs={"brokenplugin": plugin_dir}, home=meiva_home())
    assert any(
        "brokenplugin" in r["message"] and "failed to load" in r["message"] for r in loguru_records
    )


def test_absent_optional_service_face_does_not_fail_discovery(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "no_service_face"
    plugin_dir.mkdir()
    assert (
        roster_plugins.plugin_services(
            plugin_dirs={"no_service_face": plugin_dir}, home=meiva_home()
        )
        == ()
    )


@pytest.mark.parametrize("diagnostic", [False, True])
def test_failed_gate_never_produces_a_start_or_status_roster(
    loguru_records: LogRecords,
    diagnostic: bool,
    machine_identity: MachineIdentityReader,
) -> None:
    def broken_gate() -> str | None:
        raise RuntimeError("broken gate")

    service = service_spec.ServiceSpec(
        session="invalid-gate",
        cmd="noop",
        capabilities=frozenset({"gateway"}),
        requires_db=False,
        gate=broken_gate,
    )
    read = (
        spec.services_for_capabilities_annotated if diagnostic else spec.services_for_capabilities
    )
    with pytest.raises(RuntimeError, match="broken gate"):
        read(
            frozenset({"gateway"}),
            machine=machine_identity,
            host=fake_host(),
            services=(service,),
            home=meiva_home(),
        )
    assert any("roster evaluation aborted" in r["message"] for r in loguru_records)


def test_invalid_face_aborts_manifest_generation_before_any_unit_is_born(
    tmp_path: Path,
) -> None:
    from services.supervision.meiva_root_glue.manifests import build_units

    plugin_dir = tmp_path / "invalid_config"
    plugin_dir.mkdir()
    (plugin_dir / "services.py").write_text(
        "def services(*, home):\n    raise ValueError('invalid service configuration')\n"
    )
    with pytest.raises(ValueError, match="invalid service configuration"):
        build_units(
            None,
            capabilities={"gateway"},
            repo_root=tmp_path,
            plugin_dirs={"invalid_config": plugin_dir},
            home=meiva_home(),
        )
