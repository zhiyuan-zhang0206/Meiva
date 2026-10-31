# meiva:note
# The roster and its gates act on the home they are handed, not on the process's own: every case
# builds the roster for a home under `tmp_path` that is not the environment's.

from __future__ import annotations

import shlex
from functools import partial
from pathlib import Path

import pytest

from base import config
from base.cluster.machine.tests.identity import MachineIdentitySetter
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.cluster.machine.tests.identity import set_machine_identity as set_machine_identity
from base.host.system.tests.fake_commands import fake_host
from base.paths import otel_collector_binary, otel_collector_config
from base.telemetry.lgtm_local import BACKENDS, LgtmSettings, service_argv
from services.desktop.browser.profile import profile_dir
from services.supervision.healthchecks.gate import probe as gate_probe
from services.supervision.roster import composition, spec
from services.supervision.roster.service_spec import ServiceSpec


def _service(home: Path, session: str) -> ServiceSpec:
    return next(s for s in composition.build_services(home=home) if s.session == session)


def test_the_roster_binds_the_probes_and_paths_of_the_home_it_is_built_for(tmp_path: Path) -> None:
    other = tmp_path / "another-home"
    other.mkdir()

    gate = _service(other, "gate").identity_probe
    browser = _service(other, "browser").identity_probe
    collector = _service(other, "otel-collector")

    assert isinstance(gate, partial) and gate.func is gate_probe
    assert gate.keywords == {"home": other}
    assert isinstance(browser, partial)
    assert browser.args[1] == profile_dir(other)
    assert str(otel_collector_binary(other)) in collector.cmd
    assert str(otel_collector_config(other)) in collector.cmd
    assert collector.config_inputs == (otel_collector_config(other),)


def test_a_marker_in_the_handed_home_makes_it_the_observability_station(
    tmp_path: Path, set_machine_identity: MachineIdentitySetter
) -> None:
    marked, unmarked = tmp_path / "marked", tmp_path / "unmarked"
    marked.mkdir()
    unmarked.mkdir()
    (marked / "lgtm-host").touch()
    machine = set_machine_identity(role="gateway")

    marked_reason = spec.gate_reason(
        _service(marked, "loki"), machine=machine, host=fake_host(), home=marked
    )
    unmarked_reason = spec.gate_reason(
        _service(unmarked, "loki"), machine=machine, host=fake_host(), home=unmarked
    )

    assert marked_reason is None
    assert unmarked_reason == "this home is not an observability station"


@pytest.mark.parametrize(
    ("environ", "allowed"),
    [({"MEIVA_TELEMETRY_OTLP_ENDPOINT": "http://collector.invalid:4318"}, True), ({}, False)],
)
def test_the_hosts_environment_decides_whether_the_collector_override_is_explicit(
    tmp_path: Path,
    set_machine_identity: MachineIdentitySetter,
    environ: dict[str, str],
    allowed: bool,
) -> None:
    machine = set_machine_identity(role="gateway")
    collector = _service(tmp_path, "otel-collector")

    reason = spec.gate_reason(
        collector, machine=machine, host=fake_host(environ=environ), home=tmp_path
    )

    assert (reason is None) is allowed


def test_the_lgtm_backends_run_from_the_home_the_roster_is_built_for(tmp_path: Path) -> None:
    other = tmp_path / "another-home"
    other.mkdir()

    for name in BACKENDS:
        assert _service(other, name).cmd == shlex.join(
            service_argv(other, name, LgtmSettings.from_fields(config.get_field))
        )


def test_the_protocol_probes_are_bound_to_the_home_the_roster_is_built_for(tmp_path: Path) -> None:
    other = tmp_path / "another-home"
    other.mkdir()

    for session in ("insights", "exec-host", "pty-sessions"):
        probe = _service(other, session).identity_probe
        assert isinstance(probe, partial) and probe.keywords["home"] == other


def test_the_roster_discovers_plugins_in_the_home_it_is_built_for(tmp_path: Path) -> None:
    other = tmp_path / "another-home"
    other.mkdir()

    composition.build_services(home=other)

    assert (other / "plugins").is_dir()


def test_the_collector_runs_on_a_marked_home_it_is_handed(
    tmp_path: Path, set_machine_identity: MachineIdentitySetter
) -> None:
    marked = tmp_path / "marked"
    marked.mkdir()
    (marked / "lgtm-host").touch()
    machine = set_machine_identity(role="gateway")

    reason = spec.gate_reason(
        _service(marked, "otel-collector"), machine=machine, host=fake_host(), home=marked
    )

    assert reason is None


def test_the_collector_runs_on_the_home_of_a_station_unit(
    tmp_path: Path, set_machine_identity: MachineIdentitySetter
) -> None:
    station = tmp_path / "station"
    station.mkdir()
    machine = set_machine_identity(role="gateway,observability-station")

    reason = spec.gate_reason(
        _service(station, "otel-collector"), machine=machine, host=fake_host(), home=station
    )

    assert reason is None
