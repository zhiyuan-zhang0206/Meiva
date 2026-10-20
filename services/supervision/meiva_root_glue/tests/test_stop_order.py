# meiva:note
# The generated manifest declares the agent-host first, and root's whole-tree stop follows
# declaration order, so a launchd or systemd stop of root takes the agent-host down first,
# as `meiva stop` does from the same order.
# meiva:link services/agent_runner/agent_host/lifecycle/docs/graceful-exit.meiva.okf.md

from __future__ import annotations

from pathlib import Path

from services.supervision.meiva_root.manifest import UnitManifest, UnitRegistry
from services.supervision.meiva_root_glue.manifests import STOPS_FIRST, build_units
from services.supervision.roster.service_spec import ServiceSpec


def _spec(session: str) -> ServiceSpec:
    return ServiceSpec(
        session=session,
        cmd=f"python -m {session.replace('-', '_')}",
        capabilities=frozenset({"agent-runner"}),
        requires_db=False,
    )


def test_root_stops_the_agent_host_before_every_other_unit(tmp_path: Path) -> None:
    sessions = ["gateway", "alpha-sidecar", STOPS_FIRST, "ops"]
    units = build_units(
        [_spec(session) for session in sessions],
        capabilities={"agent-runner"},
        repo_root=tmp_path,
        environments={session: {} for session in sessions},
    )
    assert [unit["id"] for unit in units] == [STOPS_FIRST, "gateway", "alpha-sidecar", "ops"]
    registry = UnitRegistry([UnitManifest.from_mapping(unit, origin="test") for unit in units])
    assert registry.all_stop_order() == (STOPS_FIRST, "gateway", "alpha-sidecar", "ops")
