from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from base.clock.tests.fakes import clock_at
from base.clock.tests.seeded_rng import seeded_rng
from base.host.permissions_helper.client import PermissionsHelperClient
from base.host.permissions_helper.grants import gate
from base.host.permissions_helper.grants.catalog import PING_KEYS, PREFLIGHT_SERVICES, Grant
from base.host.permissions_helper.grants.probe import ProbeResult
from base.host.system.tests.fake_commands import DARWIN_ARM64, fake_host
from base.sessions.helperproc import helper_client
from gateway.routers import host_permissions
from services.desktop.permissions_helper.onboarding import gate_host

NOW = datetime(2026, 10, 17, 10, 0, tzinfo=UTC)


def _report(**overrides: Grant) -> gate.GateReport:
    grants = dict.fromkeys(PREFLIGHT_SERVICES, Grant.GRANTED)
    booleans = dict.fromkeys(PING_KEYS, True)
    for item_id, grant in overrides.items():
        item = gate.item_for(item_id)
        if item.ping_key:
            booleans[item.ping_key] = grant is Grant.GRANTED
        for service in item.services:
            grants[service] = grant
    return gate.evaluate(
        ProbeResult(grants=grants, booleans=booleans, supports_request=True),
        now=NOW,
    )


class FakeGate:
    def __init__(self, report: gate.GateReport) -> None:
        self._report = report
        self.requested = 0
        self.deferred = 0
        self.kwargs: dict[str, object] = {}

    def check(self) -> gate.GateReport:
        return self._report

    def onboard(self, **kwargs: object) -> gate.GateReport:
        self.requested += 1
        self.kwargs = kwargs
        return self._report

    def defer(self) -> None:
        self.deferred += 1

    def restart_helper(self) -> str:
        return ""


@pytest.fixture
def app() -> FastAPI:
    built = FastAPI()
    built.include_router(host_permissions.router)
    return built


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture
def gate_stub() -> FakeGate:
    return FakeGate(_report(**{"screen-recording": Grant.DENIED}))


def _install(app: FastAPI, stub: FakeGate) -> None:
    app.dependency_overrides[host_permissions.gate_service] = lambda: stub


def test_get_returns_the_live_checklist(
    client: TestClient, app: FastAPI, gate_stub: FakeGate
) -> None:
    _install(app, gate_stub)
    body = client.get("/api/host/permissions").json()
    assert body["state"] == "action_required"
    assert body["required_missing"] == 1
    assert body["checked_at"] == NOW.isoformat()
    row = next(item for item in body["items"] if item["id"] == "screen-recording")
    assert row["jump"].startswith("x-apple.systempreferences:")
    assert row["why"] and row["verify"]


def test_request_runs_the_gate_without_an_attestation_flag(
    client: TestClient, app: FastAPI, gate_stub: FakeGate
) -> None:
    _install(app, gate_stub)
    assert client.post("/api/host/permissions/request").json()["state"] == "action_required"
    assert gate_stub.requested == 1
    assert gate_stub.kwargs["automatic"] is False


def test_request_and_defer_go_through_the_same_domain_call(
    client: TestClient, app: FastAPI, gate_stub: FakeGate
) -> None:
    _install(app, gate_stub)
    assert client.post("/api/host/permissions/request").json()["state"] == "action_required"
    assert gate_stub.requested == 1
    assert client.post("/api/host/permissions/defer").json()["state"] == "action_required"
    assert gate_stub.deferred == 1


def _process_state(
    app: FastAPI, home: Path, *, darwin: bool, environ: dict[str, str] | None = None
) -> None:
    host = fake_host(platform=DARWIN_ARM64, environ=environ) if darwin else fake_host()
    app.state.gateway_process = SimpleNamespace(home=home, host=host)
    app.state.gateway_config = SimpleNamespace(upload_home=home / "account", rng=seeded_rng())
    app.state.clock = clock_at(NOW)


# meiva:note
# Without an override the router describes the gate from the gateway process it serves: the
# process's own home holds the record, and the pass is stamped by the app's clock. A helper that
# does not answer is `unavailable`, and the record still lands under that home.
def test_the_gate_of_a_mac_gateway_probes_through_its_process_home(
    client: TestClient, app: FastAPI, tmp_path: Path
) -> None:
    home = tmp_path / "h"
    home.mkdir()
    _process_state(app, home, darwin=True)

    body = client.get("/api/host/permissions").json()

    assert body["state"] == "unavailable"
    stored = gate.read_record(gate.record_path(home))
    assert stored is not None
    assert stored.checked_at == NOW


def test_the_gate_of_a_gateway_on_a_platform_without_a_helper_has_nothing_to_check(
    client: TestClient, app: FastAPI, tmp_path: Path
) -> None:
    _process_state(app, tmp_path, darwin=False)

    body = client.get("/api/host/permissions").json()

    assert body["state"] == "not_applicable"
    assert body["checked_at"] == NOW.isoformat()
    assert not gate.record_path(tmp_path).exists()


# meiva:note
# The service the dependency builds describes its host from the gateway process and the app's
# configuration: the process's home, the account home the app's configuration holds (not the Meiva
# home), the launch environment the process was built with, and the helper of the process's
# platform dialling the socket of that home.
def test_the_gate_service_describes_its_host_from_the_gateway_process(
    app: FastAPI, tmp_path: Path
) -> None:
    home = tmp_path / "h"
    _process_state(app, home, darwin=True, environ={"PATH": "/the/gateways/launch/path"})

    service = host_permissions.gate_service(Request({"type": "http", "app": app}))

    seat = service.host
    assert seat.home == tmp_path / "h" / "account"
    assert seat.workdir == home / "permissions-onboarding"
    assert seat.record_path == gate.record_path(home)
    assert (
        seat.env
        == gate_host(
            home=home,
            user_home=seat.home,
            environ={"PATH": "/the/gateways/launch/path"},
            helper=None,
        ).env
    )
    assert seat.env != gate_host(home=home, user_home=seat.home, environ={}, helper=None).env
    assert isinstance(seat.client, PermissionsHelperClient)
    assert seat.client.sock_path == helper_client(home=home).sock_path
