"""services.supervision.meiva_root_glue.glue / .drill: the reference and drill assemblies."""

from __future__ import annotations

import json
import os
import sys
import time
import types
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine import MachineIdentityReader
from base.config.tests.configuration import snapshot_process_config
from base.daemon.health import DaemonProbe
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import DbConfig
from services.supervision.meiva_root.health import HealthMonitor
from services.supervision.meiva_root.manifest import (
    ROOT_ID,
    RestartPolicy,
    UnitManifest,
    UnitRegistry,
)
from services.supervision.meiva_root.probes import Probe, ProbeError
from services.supervision.meiva_root.supervisor import Supervisor
from services.supervision.meiva_root.wiring import WiringContext
from services.supervision.meiva_root_glue import drill, glue
from services.supervision.meiva_root_glue.diagnostics import Diagnostic, RootHealthRounds

_SLEEPER = ["/bin/sleep", "60"]


@dataclass(slots=True)
class _Spec:
    session: str
    healthcheck_module: str | None
    identity_probe: Probe | None


def _registry(unit_ids: tuple[str, ...]) -> UnitRegistry:
    manifests = [
        UnitManifest(
            id=unit_id,
            exec=tuple(_SLEEPER),
            restart=RestartPolicy.ALWAYS,
            attach=ROOT_ID,
        )
        for unit_id in unit_ids
    ]
    return UnitRegistry(manifests)


def _context(tmp_path: Path, registry: UnitRegistry) -> WiringContext:
    log_dir = tmp_path / "logs"
    return WiringContext(
        supervisor=Supervisor(registry, run_dir=tmp_path),
        registry=registry,
        run_dir=tmp_path,
        log_dir=log_dir,
    )


def _empty_diagnostics(
    _requested: set[str],
    *,
    database: Callable[[], Database],
    machine: MachineIdentityReader,
) -> list[Diagnostic]:
    del database, machine
    return []


async def test_reference_wiring_registers_gated_specs(tmp_path: Path) -> None:
    registry = _registry(("svc-a",))
    context = _context(tmp_path, registry)
    specs = (
        _Spec("svc-a", "services.supervision.healthchecks.fake", lambda: DaemonProbe.up("fine")),
        _Spec("svc-b", None, lambda: DaemonProbe.up("not monitored")),
        _Spec("svc-c", "services.supervision.healthchecks.fake", None),
    )
    participants = glue.build_wiring(
        context,
        boot_factory=snapshot_process_config,
        diagnostics_builder=_empty_diagnostics,
        services_builder=lambda: specs,
    )
    await context.supervisor.start()
    try:
        assert len(participants) == 2
        monitor = participants[0]
        assert isinstance(monitor, RootHealthRounds)
        await monitor.run_round()
        health = cast("dict[str, dict[str, object]]", monitor.health_snapshot())
        # Only svc-a is requested by this exact root manifest.
        assert set(health) == {"svc-a", "observer:root-health"}
        assert health["observer:root-health"]["expected_since"] is None
        assert isinstance(health["observer:root-health"]["last_completed_at"], float)
        assert health["svc-a"]["last_verdict"] == "alive"
        status = await context.supervisor.status()
        assert "health" in status and "metrics" in status
    finally:
        await context.supervisor.shutdown()


async def test_static_probe_seam_resolves_lazily(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registry(("svc-host",))
    context = _context(tmp_path, registry)
    module = types.ModuleType("wiring_fixture_probe")
    setattr(module, "probe", lambda: DaemonProbe.up("static"))  # noqa: B010 - dynamic module attr
    monkeypatch.setitem(sys.modules, "wiring_fixture_probe", module)
    participants = glue.build_wiring(
        context,
        boot_factory=snapshot_process_config,
        diagnostics_builder=_empty_diagnostics,
        services_builder=lambda: (),
        static_probes={"svc-host": "wiring_fixture_probe:probe"},
    )
    await context.supervisor.start()
    try:
        monitor = participants[0]
        assert isinstance(monitor, RootHealthRounds)
        await monitor.run_round()
        health = cast("dict[str, dict[str, object]]", monitor.health_snapshot())
        assert health["svc-host"]["last_verdict"] == "alive"
    finally:
        await context.supervisor.shutdown()


async def test_unresolvable_static_probe_yields_no_verdict(tmp_path: Path) -> None:
    registry = _registry(("svc-host",))
    context = _context(tmp_path, registry)
    participants = glue.build_wiring(
        context,
        boot_factory=snapshot_process_config,
        diagnostics_builder=_empty_diagnostics,
        services_builder=lambda: (),
        static_probes={"svc-host": "no_such_probe_module:probe"},
    )
    await context.supervisor.start()
    try:
        monitor = participants[0]
        assert isinstance(monitor, RootHealthRounds)
        await monitor.run_round()  # no crash; no verdict, no action
        health = cast("dict[str, dict[str, object]]", monitor.health_snapshot())
        assert health["svc-host"]["last_verdict"] == "unavailable"
    finally:
        await context.supervisor.shutdown()


async def test_drill_wiring_verifies_claims(tmp_path: Path) -> None:
    registry = _registry(("light-a", "light-b"))
    context = _context(tmp_path, registry)
    await context.supervisor.start()
    try:
        participants = drill.build_drill_wiring(context)
        assert len(participants) == 2
        monitor = participants[0]
        assert isinstance(monitor, HealthMonitor)
        await monitor.run_round()
        health = cast("dict[str, dict[str, object]]", monitor.health_snapshot())
        assert health["light-a"]["last_verdict"] == "alive"
        assert health["light-b"]["last_verdict"] == "alive"
        # An operator stop is expected state: reported, never counted.
        await context.supervisor.down("light-b")
        await monitor.run_round()
        health = cast("dict[str, dict[str, object]]", monitor.health_snapshot())
        assert health["light-b"]["last_verdict"] == "down"
        assert health["light-b"]["restart_attempts"] == 0
    finally:
        await context.supervisor.shutdown()


class _TreeStub:
    def __init__(
        self, view: dict[str, object] | None = None, error: Exception | None = None
    ) -> None:
        self._view = view
        self._error = error

    def tree_view(self) -> dict[str, object]:
        if self._error is not None:
            raise self._error
        assert self._view is not None
        return self._view


def _view(*units: dict[str, object]) -> dict[str, object]:
    return {"root_pid": 1, "units": list(units)}


def test_drill_liveness_probe_reads_the_raw_tree() -> None:
    dead = drill.liveness_probe(
        _TreeStub(_view({"id": "u1", "state": "running", "pid": 999_999_999})), "u1"
    )
    assert dead().verdict.value == "down"  # claimed running, pid not alive

    live = drill.liveness_probe(
        _TreeStub(_view({"id": "u1", "state": "running", "pid": os.getpid()})), "u1"
    )
    assert live().verdict.value == "alive"

    stopped = drill.liveness_probe(
        _TreeStub(_view({"id": "u1", "state": "stopped", "pid": None})), "u1"
    )
    assert stopped().verdict.value == "down"

    missing = drill.liveness_probe(_TreeStub(_view()), "u1")
    assert missing().verdict.value == "down"

    broken = drill.liveness_probe(_TreeStub(error=RuntimeError("no tree")), "u1")
    assert broken().verdict.value == "unavailable"


def test_drill_heartbeat_probe_requires_freshness(tmp_path: Path) -> None:
    heartbeat_dir = tmp_path / "heartbeat"
    heartbeat_dir.mkdir()
    beat = heartbeat_dir / "u1.beat"
    beat.touch()
    probe = drill.liveness_probe(
        _TreeStub(_view({"id": "u1", "state": "running", "pid": os.getpid()})),
        "u1",
        heartbeat_dir=heartbeat_dir,
    )

    fresh = probe()
    assert fresh.verdict.value == "alive"
    assert "heartbeat" in fresh.detail

    stale_at = time.time() - 30.0
    os.utime(beat, (stale_at, stale_at))
    stale = probe()
    assert stale.verdict.value == "down"
    assert "stale" in stale.detail

    beat.unlink()
    assert probe().verdict.value == "alive"  # no heartbeat published -> liveness only


def test_reference_wiring_refuses_unobserved_manifest_unit(tmp_path: Path) -> None:
    context = _context(tmp_path, _registry(("svc-a",)))
    with pytest.raises(ProbeError, match="lack readiness probes: svc-a"):
        glue.build_wiring(
            context,
            boot_factory=snapshot_process_config,
            diagnostics_builder=_empty_diagnostics,
            services_builder=lambda: (),
        )


async def test_glue_uses_shared_readiness_tiers_for_native_startup(
    tmp_path: Path,
) -> None:
    from base.deploy.progress_timeout import (
        NON_CRITICAL_SERVICE_READY_TIMEOUT_S,
        SERVICE_READY_TIMEOUT_S,
    )

    context = _context(tmp_path, _registry(("gate", "labeler")))
    specs = tuple(
        _Spec(name, "probe", lambda: DaemonProbe.down("starting")) for name in ("gate", "labeler")
    )
    rounds = glue.build_wiring(
        context,
        boot_factory=snapshot_process_config,
        diagnostics_builder=_empty_diagnostics,
        services_builder=lambda: specs,
    )[0]
    await context.supervisor.start()
    try:
        assert isinstance(rounds, RootHealthRounds)
        await rounds.run_round()
        health = cast("dict[str, dict[str, object]]", rounds.health_snapshot())
        for name, budget in (
            ("gate", SERVICE_READY_TIMEOUT_S),
            ("labeler", NON_CRITICAL_SERVICE_READY_TIMEOUT_S),
        ):
            assert budget - 5 < cast(float, health[name]["startup_remaining_s"]) <= budget
            assert health[name]["last_verdict"] == "down"
            assert health[name]["restart_attempts"] == 0
    finally:
        await context.supervisor.shutdown()


def test_launched_services_reads_the_root_manifest(tmp_path: Path) -> None:
    from services.supervision.meiva_root_glue.manifests import launched_services

    path = tmp_path / "manifests.json"
    # meiva:note
    # nothing was ever launched
    assert launched_services(path) is None
    units = [{"id": name, "exec": ["true"], "restart": "never"} for name in ("gateway", "frontend")]
    path.write_text(json.dumps({"units": units}), encoding="utf-8")
    assert launched_services(path) == frozenset({"gateway", "frontend"})


def test_static_probe_references_are_read_only() -> None:
    import operator

    with pytest.raises(TypeError):
        operator.setitem(cast(Any, glue.STATIC_PROBES), "svc-host", "wiring_fixture_probe:probe")


async def test_reference_station_and_writer_share_one_entry_gate(
    tmp_path: Path,
    database: Database,
) -> None:
    gates: list[ProcessDbGate] = []

    readers: list[Callable[[], str]] = []
    machines: list[MachineIdentityReader] = []

    def make_database(
        config: DbConfig, *, gate: ProcessDbGate, direct_url: ClusterDirectUrl
    ) -> Database:
        del config
        gates.append(gate)
        readers.append(direct_url.local_host)
        return database

    def diagnostics(
        _requested: set[str], *, database: Callable[[], Database], machine: MachineIdentityReader
    ) -> list[Diagnostic]:
        machines.append(machine)
        assert database() is database_handle
        assert database() is database_handle
        return []

    database_handle = database
    rounds = glue.build_wiring(
        _context(tmp_path, _registry(())),
        boot_factory=snapshot_process_config,
        database_builder=make_database,
        diagnostics_builder=diagnostics,
        services_builder=lambda: (),
    )[0]
    assert isinstance(rounds, RootHealthRounds)
    assert len(gates) == 2 and gates[0] is gates[1]
    assert len(machines) == 1
    assert readers == [machines[0].host, machines[0].host]
    await rounds.stop()


# meiva:note
# The agent-host memory budget is opt-in and platform-gated. A cap that does nothing is
# worse than no cap: it lets an operator believe the plane is bounded. These tests pin
# the three ways it declines (zero, non-Linux, no usable systemd) and the one way it
# applies, through explicit parameters rather than patched module state.
def test_agent_host_cap_is_off_by_default() -> None:
    assert (
        glue.agent_host_memory_caps(
            max_mb=0,
            swap_max_mb=0,
            platform="linux",
            scope_available=lambda _max_mb, _swap_mb: True,
        )
        == {}
    )


def test_agent_host_cap_declines_where_cgroups_do_not_exist() -> None:
    assert (
        glue.agent_host_memory_caps(
            max_mb=18432,
            swap_max_mb=0,
            platform="darwin",
            scope_available=lambda _max_mb, _swap_mb: True,
        )
        == {}
    )


def test_agent_host_cap_declines_when_systemd_cannot_scope() -> None:
    assert (
        glue.agent_host_memory_caps(
            max_mb=18432,
            swap_max_mb=0,
            platform="linux",
            scope_available=lambda _max_mb, _swap_mb: False,
        )
        == {}
    )


def test_agent_host_cap_applies_when_every_precondition_holds() -> None:
    caps = glue.agent_host_memory_caps(
        max_mb=18432,
        swap_max_mb=0,
        platform="linux",
        scope_available=lambda _max_mb, _swap_mb: True,
    )
    assert caps == {"agent-host": glue.UnitMemoryCap(max_mb=18432, swap_max_mb=0)}


def test_the_launch_rewrite_wraps_only_the_capped_unit() -> None:
    caps = {"agent-host": glue.UnitMemoryCap(max_mb=18432, swap_max_mb=0)}
    argv = ["python", "-m", "services.agent_runner.agent_host"]
    assert glue.memory_scoped_argv("agent-host", argv, caps=caps) == [
        "systemd-run",
        "--user",
        "--scope",
        "--collect",
        "-p",
        "MemoryMax=18432M",
        "-p",
        "MemorySwapMax=0M",
        "--",
        *argv,
    ]
    assert glue.memory_scoped_argv("gateway", argv, caps=caps) == argv
