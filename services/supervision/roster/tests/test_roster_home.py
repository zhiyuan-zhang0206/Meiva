# meiva:note
# The roster and its gates act on the home they are handed, not on the process's own: every case
# builds the roster for a home under `tmp_path` that is not the environment's.

from __future__ import annotations

from functools import partial
from pathlib import Path

import pytest

from base.cluster.machine.tests.identity import MachineIdentitySetter
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.cluster.machine.tests.identity import set_machine_identity as set_machine_identity
from base.host.system.tests.fake_commands import fake_host
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
    assert str(other) in collector.cmd


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
