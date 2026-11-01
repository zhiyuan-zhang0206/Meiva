from __future__ import annotations

from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.host.system.tests.fake_commands import fake_host
from base.paths import meiva_home
from services.agent_runner.pty_sessions import shutdown_budget
from services.supervision.roster import composition, service_spec, spec
from services.supervision.roster.service_spec import ServiceSpec


def _spec() -> service_spec.ServiceSpec:
    return next(
        s for s in composition.build_services(home=meiva_home()) if s.session == "pty-sessions"
    )


def test_both_a_gateway_host_and_a_runner_host_run_the_service(
    *, machine_identity: MachineIdentityReader
) -> None:
    declared = _spec()
    assert declared.capabilities == frozenset({"gateway", "agent-runner"})
    for role in ("gateway", "agent-runner"):
        started = {
            s.session
            for s in spec.services_for_capabilities(
                frozenset({role}), machine=machine_identity, host=fake_host(), home=meiva_home()
            )
        }
        assert "pty-sessions" in started, role


def test_the_service_needs_no_database_and_no_profile_marker() -> None:
    declared = _spec()
    assert declared.requires_db is False
    assert service_spec.db_access(declared) is None
    assert service_spec.api_access(declared) is None
    assert service_spec.profile_marker(declared) is None


# meiva:note
# A shell inherits this service's environment, so the machine token must be the host's class: one
# fixed class cannot launch on both kinds of host (task #5979).
def test_the_machine_token_class_is_the_hosts_own() -> None:
    declared = _spec()
    assert declared.api_access_from_host is True
    assert service_spec.api_access_for_host(declared, frozenset({"agent-runner"})) == "runner"
    assert service_spec.api_access_for_host(declared, frozenset({"gateway"})) == "gateway"
    # meiva:note
    # A host that carries agent-runner can only ever issue the runner class, a single box too.
    both = frozenset({"gateway", "agent-runner"})
    assert service_spec.api_access_for_host(declared, both) == "runner"
    # meiva:note
    # A host that runs neither capability does not run this service at all.
    neither = frozenset({"observability-station"})
    assert service_spec.api_access_for_host(declared, neither) is None


def test_a_declared_class_still_wins_over_the_host_roles() -> None:
    declared = ServiceSpec(
        session="unit-api-service",
        cmd="x",
        capabilities=service_spec.BOTH,
        requires_db=False,
        db_access="gateway",
        api_access_from_host=True,
    )
    for roles in ("gateway", "agent-runner"):
        assert service_spec.api_access_for_host(declared, frozenset({roles})) == "gateway"


def test_the_ownership_probe_is_bound_to_the_roots_generation() -> None:
    declared = _spec()
    assert declared.identity_probe is not None
    assert declared.curl_url is None and declared.tcp_port is None, "a Unix endpoint binds its peer"
    assert declared.cmd == ".venv/bin/python -m services.agent_runner.pty_sessions.daemon"


def test_the_stop_window_covers_the_services_own_closure() -> None:
    assert _spec().stop_ceiling_s == shutdown_budget.SHUTDOWN_CEILING_S
    assert shutdown_budget.SHUTDOWN_CEILING_S > (
        shutdown_budget.SHUTDOWN_HANGUP_WAIT_S + shutdown_budget.SHUTDOWN_KILL_S
    )
