from __future__ import annotations

from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.host.system.tests.fake_commands import fake_host
from services.agent_runner.exec_host import shutdown_budget
from services.supervision.roster import composition, service_spec, spec


def _spec() -> service_spec.ServiceSpec:
    return next(s for s in composition.build_services() if s.session == "exec-host")


def test_only_an_agent_runner_host_runs_exec_host(
    *, machine_identity: MachineIdentityReader
) -> None:
    declared = _spec()
    assert declared.capabilities == frozenset({"agent-runner"})
    runner_started = {
        s.session
        for s in spec.services_for_capabilities(
            frozenset({"agent-runner"}), machine=machine_identity, host=fake_host()
        )
    }
    gateway_started = {
        s.session
        for s in spec.services_for_capabilities(
            frozenset({"gateway"}), machine=machine_identity, host=fake_host()
        )
    }
    assert "exec-host" in runner_started
    assert "exec-host" not in gateway_started


def test_exec_host_uses_agent_profile_and_runner_db_access() -> None:
    declared = _spec()
    assert declared.requires_db is False
    assert service_spec.db_access(declared) == "runner"
    assert service_spec.profile_marker(declared) == "agent"


def test_exec_host_probe_and_command_wiring() -> None:
    declared = _spec()
    assert declared.identity_probe is not None
    assert declared.curl_url is None and declared.tcp_port is None
    assert declared.cmd == ".venv/bin/python -m services.agent_runner.exec_host.daemon"


def test_exec_host_stop_window_covers_drain_budget() -> None:
    assert _spec().stop_ceiling_s == shutdown_budget.SHUTDOWN_CEILING_S
    assert shutdown_budget.SHUTDOWN_CEILING_S > (
        shutdown_budget.SHUTDOWN_TERM_WAIT_S + shutdown_budget.SHUTDOWN_KILL_S
    )
