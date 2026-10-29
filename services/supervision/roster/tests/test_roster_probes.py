from __future__ import annotations

import asyncio
import os
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from base.config import get_field, set_field
from base.daemon.health import ProbeVerdict, start_health_server, stop_health_server
from base.daemon.tests.health_support import unknown_image
from base.host.net.tests.local_http import serve_json
from base.paths import meiva_home
from services.desktop.browser.orphan import is_cluster_chrome
from services.desktop.browser.profile import profile_dir
from services.supervision.healthchecks.gate import probe as gate_probe
from services.supervision.roster import composition
from services.supervision.roster.service_spec import ServiceSpec


def _service(session: str) -> ServiceSpec:
    return next(
        service
        for service in composition.build_services(home=meiva_home())
        if service.session == session
    )


@pytest.mark.parametrize("same_home", [True, False])
def test_gateway_roster_probe_checks_the_answering_units_home(
    unit_home: Path, same_home: bool
) -> None:
    original_url = get_field("gateway_health_url")
    home = unit_home if same_home else unit_home / "another-unit"
    try:
        with serve_json({"status": "ok", "home": str(home)}) as service:
            set_field("gateway_health_url", service.url)
            declared = _service("gateway")
            assert declared.identity_probe is not None
            verdict = declared.identity_probe()
        assert verdict.alive is same_home
        assert verdict.terminal is not same_home
    finally:
        set_field("gateway_health_url", original_url)


async def test_plugin_roster_probe_accepts_its_real_health_daemon(unit_home: Path) -> None:
    original_port = get_field("task_maintenance_health_port")
    pidfile = unit_home / "run" / "task_maintenance.pid"
    server = await start_health_server(
        "task_maintenance", home=meiva_home(), port=0, image=unknown_image()
    )
    try:
        port = server.sockets[0].getsockname()[1]
        set_field("task_maintenance_health_port", port)
        pidfile.parent.mkdir(parents=True, exist_ok=True)
        pidfile.write_text(str(os.getpid()))
        declared = _service("task-maintenance")
        assert declared.identity_probe is not None
        verdict = await asyncio.to_thread(declared.identity_probe)
        assert verdict.alive is True
        assert verdict.terminal is False
    finally:
        await stop_health_server(server)
        set_field("task_maintenance_health_port", original_port)
        pidfile.unlink(missing_ok=True)


def test_browser_roster_does_not_accept_a_foreign_cdp_responder(unit_home: Path) -> None:
    assert profile_dir(unit_home) == unit_home / "chrome-profile"
    original_port = get_field("browser_cdp_port")
    try:
        with serve_json({"Browser": "Chrome/150.0.0.0"}) as service:
            port = urlsplit(service.url).port
            assert port is not None
            set_field("browser_cdp_port", port)
            declared = _service("browser")
            assert declared.identity_probe is not None
            verdict = declared.identity_probe()
        assert verdict.verdict is ProbeVerdict.PORT_TAKEN
    finally:
        set_field("browser_cdp_port", original_port)


@pytest.mark.parametrize("same_profile", [True, False])
def test_browser_identity_requires_the_units_positive_profile_fact(
    unit_home: Path, same_profile: bool
) -> None:
    profile = unit_home / "chrome-profile"
    observed = profile if same_profile else unit_home / "another-profile"
    assert is_cluster_chrome("Chrome", [f"--user-data-dir={observed}"], profile) is same_profile


def test_browser_identity_is_the_profile_probe_not_a_curl(unit_home: Path) -> None:
    declared = _service("browser")
    probe = declared.identity_probe
    assert isinstance(probe, partial)
    assert probe.func is composition.browser_probe
    assert probe.args == (int(get_field("browser_cdp_port")), profile_dir(unit_home))


# meiva:note
# A Gate that answers for another home holds the port; the probe must be asked about this unit's
# home or the root would judge its own Gate foreign.
def test_gate_identity_is_the_gate_probe_of_the_units_home(unit_home: Path) -> None:
    probe = _service("gate").identity_probe
    assert isinstance(probe, partial)
    assert probe.func is gate_probe
    assert probe.keywords == {"home": unit_home}
