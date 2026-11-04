# meiva:note
# The generated manifest declares the agent-host first, and root's whole-tree stop follows
# declaration order, so a launchd or systemd stop of root takes the agent-host down first,
# as `meiva stop` does from the same order.
# meiva:link services/agent_runner/agent_host/lifecycle/docs/graceful-exit.meiva.okf.md

from __future__ import annotations

from pathlib import Path

from base.paths import meiva_home
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
        home=meiva_home(),
    )
    assert [unit["id"] for unit in units] == [STOPS_FIRST, "gateway", "alpha-sidecar", "ops"]
    registry = UnitRegistry([UnitManifest.from_mapping(unit, origin="test") for unit in units])
    assert registry.all_stop_order() == (STOPS_FIRST, "gateway", "alpha-sidecar", "ops")


def test_otel_collector_stops_after_producers_and_before_storage_backends(tmp_path: Path) -> None:
    sessions = [
        "loki",
        "otel-collector",
        "gateway",
        "tempo",
        STOPS_FIRST,
        "pty-sessions",
        "prometheus",
    ]
    units = build_units(
        [_spec(session) for session in sessions],
        capabilities={"agent-runner"},
        repo_root=tmp_path,
        environments={session: {} for session in sessions},
        home=meiva_home(),
    )
    assert [unit["id"] for unit in units] == [
        STOPS_FIRST,
        "gateway",
        "pty-sessions",
        "otel-collector",
        "loki",
        "tempo",
        "prometheus",
    ]


def test_browser_mcp_stops_before_browser_collector_and_storage_backends(tmp_path: Path) -> None:
    sessions = [
        "tempo",
        "browser",
        "otel-collector",
        "gateway",
        "loki",
        "browser-mcp",
        STOPS_FIRST,
        "prometheus",
        "ops",
    ]
    units = build_units(
        [_spec(session) for session in sessions],
        capabilities={"agent-runner"},
        repo_root=tmp_path,
        environments={session: {} for session in sessions},
        home=meiva_home(),
    )
    assert [(unit["id"], unit["stop_tier"]) for unit in units] == [
        (STOPS_FIRST, 0),
        ("gateway", 1),
        ("browser-mcp", 1),
        ("ops", 1),
        ("browser", 2),
        ("otel-collector", 3),
        ("tempo", 4),
        ("loki", 4),
        ("prometheus", 4),
    ]
    registry = UnitRegistry([UnitManifest.from_mapping(unit, origin="test") for unit in units])
    assert registry.all_stop_waves() == (
        (STOPS_FIRST,),
        ("gateway", "browser-mcp", "ops"),
        ("browser",),
        ("otel-collector",),
        ("tempo", "loki", "prometheus"),
    )
