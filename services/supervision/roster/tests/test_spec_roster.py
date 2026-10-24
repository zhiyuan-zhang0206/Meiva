from __future__ import annotations

import json
import os
import subprocess
import sys
from functools import partial
from pathlib import Path
from typing import cast

import pytest

from base.cluster.machine import MachineIdentity, MachineRole
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import settings
from base.daemon.endpoints import ServiceEndpoints
from base.daemon.health import DaemonProbe, probe_daemon
from base.host.system import probes
from base.host.system.tests.fake_commands import fake_host
from base.paths.tests.homes import unit_home as unit_home
from services.supervision import roster
from services.supervision.roster import composition, service_spec, spec

_CHILD = Path(__file__).with_name("gate_roster_child.py")
_REPO = Path(__file__).resolve().parents[4]
_GATEWAY_SESSIONS = {
    "gate",
    "loki",
    "prometheus",
    "grafana",
    "gateway",
    "im-bridge",
    "labeler",
    "heartbeat",
    "delivery-watchdog",
    "events-maintenance",
    "task-maintenance",
    "memory-search",
    "memory-indexer",
    "frontend",
    "otel-collector",
    "pty-sessions",
    "pg-backup",
    "redis-bridge",
    "ttl-reaper",
    "schedule-manager",
    "insights",
}
_AGENT_RUNNER_SESSIONS = {
    "loki",
    "prometheus",
    "grafana",
    "page-server",
    "ops",
    "agent-host",
    "exec-host",
    "browser",
    "browser-mcp",
    "mcp-daemon",
    "computer-mcp",
    "otel-collector",
    "pty-sessions",
}


def test_agent_host_spec_launches_under_the_agent_profile() -> None:
    agent_host = next(s for s in composition.build_services() if s.session == "agent-host")
    assert agent_host.profile == "agent"
    assert service_spec.profile_marker(agent_host) == "agent"


def test_profile_override_wins_over_derivation_and_no_marker() -> None:
    derived = service_spec.ServiceSpec(
        session="x",
        cmd="true",
        capabilities=cast(frozenset[MachineRole], frozenset({"agent-runner"})),
        requires_db=False,
    )
    assert service_spec.profile_marker(derived) == "runner"
    overridden = service_spec.ServiceSpec(
        session="x",
        cmd="true",
        capabilities=cast(frozenset[MachineRole], frozenset({"agent-runner"})),
        requires_db=False,
        profile="agent",
        no_profile_marker=True,
    )
    assert service_spec.profile_marker(overridden) == "agent"


def test_every_service_declares_non_empty_capabilities() -> None:
    for s in composition.build_services():
        assert s.capabilities, f"{s.session} declares no capabilities"
        assert s.capabilities <= {"gateway", "agent-runner", "observability-station"}


def test_capability_partition() -> None:
    by_session = {s.session: s for s in composition.build_services()}
    assert set(by_session) == _GATEWAY_SESSIONS | _AGENT_RUNNER_SESSIONS
    for session, s in by_session.items():
        on_gateway = "gateway" in s.capabilities
        on_runner = "agent-runner" in s.capabilities
        assert on_gateway == (session in _GATEWAY_SESSIONS)
        assert on_runner == (session in _AGENT_RUNNER_SESSIONS)


@pytest.mark.parametrize(
    ("role", "expected"),
    [("gateway", _GATEWAY_SESSIONS), ("agent-runner", _AGENT_RUNNER_SESSIONS)],
)
def test_annotated_roster_membership_per_capability(
    role: str, expected: set[str], *, machine_identity: MachineIdentity
) -> None:
    got = {
        s.session
        for s, _reason in spec.services_for_capabilities_annotated(
            frozenset({role}), machine=machine_identity, host=fake_host()
        )
    }
    assert got == expected


def test_gateway_roster_ordering_is_load_bearing() -> None:
    order = [s.session for s in composition.build_services()]
    assert order.index("memory-search") < order.index("memory-indexer")


def test_agent_runner_roster_ordering(*, machine_identity: MachineIdentity) -> None:
    order = [
        s.session
        for s, _r in spec.services_for_capabilities_annotated(
            frozenset({"agent-runner"}), machine=machine_identity, host=fake_host()
        )
    ]
    assert order.index("agent-host") < order.index("ops")


def test_application_roster_has_one_supervision_authority() -> None:
    services = {service.session: service for service in composition.build_services()}
    assert "gateway-watchdog" not in services
    assert "agent-runner-watchdog" not in services
    assert services["delivery-watchdog"].identity_probe is not None
    assert all("services.watchdog.daemon" not in service.cmd for service in services.values())


def test_station_roster_contains_only_lgtm_with_host_enablement_gate(
    unit_home: Path, *, machine_identity: MachineIdentity
) -> None:
    rows = spec.services_for_capabilities_annotated(
        frozenset({"observability-station"}), machine=machine_identity, host=fake_host()
    )
    assert {service.session for service, _reason in rows} == {"loki", "prometheus", "grafana"}
    assert all(reason is not None for _service, reason in rows)

    (unit_home / "lgtm-host").touch()

    assert {
        service.session
        for service in spec.services_for_capabilities(
            frozenset({"observability-station"}), machine=machine_identity, host=fake_host()
        )
    } == {"loki", "prometheus", "grafana"}


def test_browser_family_is_gated_by_the_platform_when_enabled(
    *, machine_identity: MachineIdentity
) -> None:
    host = fake_host()
    expected = probes.browser_incapability(settings.services.chrome_binary, host=host)
    assert expected is not None

    annotated = {
        s.session: r
        for s, r in spec.services_for_capabilities_annotated(
            frozenset({"agent-runner"}), machine=machine_identity, host=host
        )
    }

    assert annotated["browser"] == expected
    assert annotated["browser-mcp"] == expected


# meiva:note
# The roster gates evaluated by a child process that has `env` as its environment and `home` as
# its home, for the roles given. The gates read process settings and the environment.
def _gates_in_child(
    home: Path, roles: str, env: dict[str, str] | None = None
) -> dict[str, str | None]:
    base_env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR") if key in os.environ}
    done = subprocess.run(  # noqa: S603
        [sys.executable, str(_CHILD), roles],
        cwd=_REPO,
        env={**base_env, "MEIVA_HOME": str(home), **(env or {})},
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


# meiva:note
# A host-scope flag reaches a process through its environment; a cluster-scope one only through
# the unit's own `.env`, so each case is delivered the way its scope is read.
@pytest.mark.parametrize(
    ("flag", "sessions", "wording", "scope"),
    [
        ("MEIVA_BROWSER_ENABLED", ("browser", "browser-mcp"), "MEIVA_BROWSER_ENABLED", "host"),
        (
            "MEIVA_IM_BRIDGE_ENABLED",
            ("im-bridge",),
            "disabled (MEIVA_IM_BRIDGE_ENABLED off)",
            "cluster",
        ),
    ],
)
def test_a_session_whose_flag_is_off_is_gated_out_with_the_flag_named(
    tmp_path: Path, flag: str, sessions: tuple[str, ...], wording: str, scope: str
) -> None:
    roles = "agent-runner" if "browser" in sessions else "gateway"
    if scope == "cluster":
        (tmp_path / ".env").write_text(f"{flag}=false\n")

    annotated = _gates_in_child(tmp_path, roles, {flag: "false"} if scope == "host" else None)

    for session in sessions:
        reason = annotated[session]
        assert reason is not None and wording in reason


def test_otel_collector_gated_on_gateway_without_lgtm_marker(tmp_path: Path) -> None:
    annotated = _gates_in_child(tmp_path, "gateway")

    reason = annotated["otel-collector"]
    assert reason is not None
    assert "lgtm-host" in reason


def test_otel_collector_runs_on_gateway_with_lgtm_marker(tmp_path: Path) -> None:
    (tmp_path / "lgtm-host").touch()

    assert _gates_in_child(tmp_path, "gateway")["otel-collector"] is None


def test_otel_collector_runs_on_gateway_without_marker_when_endpoint_override_explicit(
    tmp_path: Path,
) -> None:
    annotated = _gates_in_child(
        tmp_path, "gateway", {"MEIVA_TELEMETRY_OTLP_ENDPOINT": "http://collector.invalid:4318"}
    )

    assert annotated["otel-collector"] is None


def test_otel_collector_runs_on_pure_runner(tmp_path: Path) -> None:
    assert _gates_in_child(tmp_path, "agent-runner")["otel-collector"] is None


def test_every_service_declares_an_identity_probe() -> None:
    missing = {s.session for s in composition.build_services() if s.identity_probe is None}
    assert missing == set(), f"{sorted(missing)} have no identity-bound health probe"


def test_daemon_identity_binds_the_probe_to_one_daemons_facts(tmp_path: Path) -> None:
    pidfile = tmp_path / "ops.pid"

    bound = cast("partial[DaemonProbe]", roster.daemon_identity("ops", pidfile))

    assert bound.func is probe_daemon
    name, url = bound.args
    assert name == "ops"
    assert str(ServiceEndpoints.from_settings().of("ops").health_port) in url
    assert bound.keywords == {"pidfile": pidfile}


@pytest.mark.parametrize(
    ("caps", "expected"),
    [
        (frozenset({"gateway"}), "gateway"),
        (frozenset({"agent-runner"}), "runner"),
        (frozenset({"gateway", "agent-runner"}), None),
    ],
)
def test_profile_marker_derives_from_capabilities(
    caps: frozenset[str], expected: str | None
) -> None:
    s = service_spec.ServiceSpec(
        session="x",
        cmd="true",
        capabilities=cast(frozenset[MachineRole], caps),
        requires_db=False,
    )
    assert service_spec.profile_marker(s) == expected


def test_labeler_is_the_only_service_that_opts_out_of_the_profile_marker() -> None:
    by_session = {s.session: s for s in composition.build_services()}
    opted_out = {sess for sess, s in by_session.items() if s.no_profile_marker}
    assert opted_out == {"labeler"}, (
        f"expected only the labeler to opt out of the profile marker, got {opted_out}"
    )
    assert service_spec.profile_marker(by_session["labeler"]) is None
    for sess, s in by_session.items():
        if sess == "labeler":
            continue
        assert service_spec.profile_marker(s) is not None or (
            "agent-runner" in s.capabilities and "gateway" in s.capabilities
        ), (
            f"{sess} must keep its MEIVA_PROCESS_PROFILE marker (provider keys stay "
            "out of its process)"
        )


def test_gateway_daemons_keep_the_gateway_marker() -> None:
    by_session = {s.session: s for s in composition.build_services()}
    for sess in ("heartbeat", "events-maintenance", "im-bridge", "memory-indexer", "gateway"):
        assert service_spec.profile_marker(by_session[sess]) == "gateway", sess


def test_runner_daemons_keep_the_runner_marker() -> None:
    by_session = {s.session: s for s in composition.build_services()}
    for sess in ("ops", "page-server"):
        assert service_spec.profile_marker(by_session[sess]) == "runner", sess
