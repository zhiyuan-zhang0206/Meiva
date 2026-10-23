"""`meiva start` probes its daemon health ports before it binds them.

The defect these cover (issue #977): a health port was treated as a per-CLUSTER
fact, but the collision domain is one MACHINE's localhost namespace. Two
agent-runners of the SAME cluster on one machine sharing a loopback namespace
were therefore handed identical ports by construction. One unit's watchdog
probed its own port, was answered by the other unit's daemon, and logged
identity mismatches, each ending "manual intervention needed".

No arithmetic scheme closes that: a later install can bind anything.
Detection is the mechanism that does not require everyone to have agreed in
advance, so the start asks each port who is there while it is still cheap to stop.

What is asserted here is the discrimination, because a gate that refuses too
eagerly is worse than none:

- a port answered by ANOTHER unit's daemon stops the start, before anything is
  launched, naming the occupant and the remedy;
- this unit's own daemon (an idempotent restart), a stray of its own home, and a
  cold port all pass;
- only the daemon health ports are gated — the gateway, the browser and the
  frontend are somebody else's problem, and the browser deliberately tolerates
  another unit's Chrome.
"""

from __future__ import annotations

import pytest

from base.daemon.health import DaemonProbe
from cli.commands import probe
from cli.commands.lifecycle.start import refuse_occupied_health_ports
from cli.commands.lifecycle.tests.startup.start_harness import StartRun
from services.supervision.roster.service_spec import AGENT_RUNNER, GATEWAY, ServiceSpec

# The gate IS the subject here, so stand the global autouse net down for this
# module (cli/commands/tests/health_port_guard.py:_guard_health_port_gate reports every port free).
pytestmark = pytest.mark.real_health_port_gate


# meiva:note
# A daemon whose probe target is an Meiva `/healthz` (a daemon health port); its identity
# probe answers `answer`, the verdict a dial of that port would have produced.
def _healthz_spec(service: str, port: int, answer: DaemonProbe) -> ServiceSpec:
    return ServiceSpec(
        session=service,
        cmd="x",
        capabilities=AGENT_RUNNER,
        requires_db=True,
        curl_url=f"http://localhost:{port}/healthz",
        health_name=service.replace("-", "_"),
        identity_probe=lambda: answer,
    )


# meiva:note
# A service whose port is NOT in the health-port block (the gateway's `/api/health`, the
# browser's CDP `/json/version`, the frontend).
def _other_endpoint_spec(service: str, url: str, answer: DaemonProbe) -> ServiceSpec:
    return ServiceSpec(
        session=service,
        cmd="x",
        capabilities=GATEWAY,
        requires_db=False,
        curl_url=url,
        identity_probe=lambda: answer,
    )


_FOREIGN = (
    "identity mismatch on http://localhost:8103/healthz: home='/home/meiva/.meiva' != "
    "'/home/other/.meiva' \u2014 another unit's daemon holds this port"
)


# meiva:note
# ─── which verdicts are conflicts ───────────────────────────────────────────────


def test_a_foreign_units_daemon_on_a_health_port_is_a_conflict() -> None:
    labeler = _healthz_spec("labeler", 8103, DaemonProbe.port_taken(_FOREIGN))

    occupied = probe.occupied_health_ports((labeler,))

    assert [o.spec.session for o in occupied] == ["labeler"]
    assert occupied[0].detail == _FOREIGN


# meiva:note
# The idempotent case. `meiva start` over a healthy host re-probes every port it is about
# to use and finds its own daemons: a start that refused here would make restart impossible on
# exactly the hosts that are working.
def test_our_own_running_daemon_is_not_a_conflict() -> None:
    labeler = _healthz_spec("labeler", 8103, DaemonProbe.up("pid 4242"))

    assert probe.occupied_health_ports((labeler,)) == ()


# meiva:note
# DOWN covers both a port nothing answers on and a stray of our OWN home (`probe_daemon`
# reaches the pid arm only after name and home matched). Both are cleared by the launch that
# follows, so neither may stop the start.
def test_a_dead_or_cold_port_is_not_a_conflict() -> None:
    labeler = _healthz_spec(
        "labeler", 8103, DaemonProbe.down("healthz unreachable: URLError: refused")
    )
    ops = _healthz_spec(
        "ops", 8113, DaemonProbe.down("healthz pid=9 != pidfile pid=8 \u2014 a stray process")
    )

    assert probe.occupied_health_ports((labeler, ops)) == ()


# meiva:note
# A terminal verdict on the gateway or the browser is not this gate's business, and folding
# them in would be a regression: the browser healthcheck deliberately tolerates another unit's
# Chrome on the CDP port, so a start that refused would leave a headed box unable to come up.
def test_only_daemon_health_ports_are_gated() -> None:
    gateway = _other_endpoint_spec(
        "gateway", "http://localhost:8000/api/health", DaemonProbe.port_taken("another gateway")
    )
    browser = _other_endpoint_spec(
        "browser", "http://localhost:9222/json/version", DaemonProbe.port_taken("another Chrome")
    )
    labeler = _healthz_spec("labeler", 8103, DaemonProbe.up("pid 1"))

    assert probe.occupied_health_ports((gateway, browser, labeler)) == ()


def test_every_conflicting_port_is_reported_not_just_the_first() -> None:
    specs = (
        _healthz_spec("labeler", 8103, DaemonProbe.port_taken("a")),
        _healthz_spec("ops", 8113, DaemonProbe.port_taken("b")),
    )

    assert [o.spec.session for o in probe.occupied_health_ports(specs)] == ["labeler", "ops"]


# meiva:note
# ─── what the start does with a conflict ───────────────────────────────────────────


def test_a_conflict_on_a_core_daemon_refuses_the_start_and_names_the_remedy(
    capsys: pytest.CaptureFixture[str],
) -> None:
    agent_host = _healthz_spec("agent-host", 8113, DaemonProbe.port_taken(_FOREIGN))

    assert refuse_occupied_health_ports((agent_host,)) == 1

    message = "".join(capsys.readouterr())
    assert "/home/meiva/.meiva" in message
    assert "Stop the listed daemon" in message
    assert "--disable-service agent-host" in message


def test_a_clear_roster_starts_normally(capsys: pytest.CaptureFixture[str]) -> None:
    agent_host = _healthz_spec("agent-host", 8113, DaemonProbe.down("cold"))

    assert refuse_occupied_health_ports((agent_host,)) == 0
    assert capsys.readouterr().err == ""


def test_an_optional_port_conflict_does_not_block_a_core_start() -> None:
    labeler = _healthz_spec("labeler", 8103, DaemonProbe.port_taken(_FOREIGN))

    assert refuse_occupied_health_ports((labeler,)) == 0


# meiva:note
# The gate probes the roster this start launches: a daemon the operator disabled cannot
# block it.
def test_a_disabled_service_is_not_in_the_roster_the_gate_probes(start_run: StartRun) -> None:
    roles = frozenset({"gateway"})
    run = start_run

    full = {spec.session for spec in run.roster(roles)}
    narrowed = {spec.session for spec in run.roster(roles, disabled=("labeler",))}

    assert "labeler" in full
    assert narrowed == full - {"labeler"}
