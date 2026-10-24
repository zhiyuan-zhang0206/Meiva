# meiva:note
# `/api/cluster/*` endpoint tests: the gateway's own status, the retired deployment routes and the
# stop announcement, each against the real route, the real home and the real tables.

from __future__ import annotations

import psycopg
import pytest

from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import MachineIdentitySetter
from base.cluster.machines import register_self
from base.db import Database
from base.deploy.lifecycle import start_serving
from base.deploy.lifecycle.tests.serving_root import serving_root as serving_root
from gateway.app import openapi_schema
from gateway.tests.auth.gateway_config import gateway_test_client
from gateway.tests.routers.cluster_endpoints.cluster_support import (
    serving_gate as serving_gate,
)


@pytest.fixture(autouse=True)
def truncate_machines(db_conn: psycopg.Connection) -> None:
    with db_conn.cursor() as cur:
        cur.execute("TRUNCATE machines, machine_units")
    db_conn.commit()


# meiva:note
# `/api/cluster/status` answers 200 for a pure gateway and reads its serving gate: a stop clears
# the gate, and the machine reports `startup` from then on.
@pytest.mark.usefixtures("serving_gate")
def test_a_pure_gateway_reports_startup_once_a_stop_has_begun(
    set_machine_identity: MachineIdentitySetter,
) -> None:
    machine: MachineIdentity = set_machine_identity(role="gateway", name="test-mc")
    with gateway_test_client(machine=machine) as client:
        serving = client.get("/api/cluster/status").json()
        start_serving.clear_serving()
        r = client.get("/api/cluster/status")
    assert serving["paused"] is False
    assert serving["paused_reason"] is None
    assert r.status_code == 200
    body = r.json()
    assert body["paused"] is True
    assert body["paused_reason"] == "startup"
    assert body["machine_name"] == "test-mc"
    assert body["serve_gateway"] is True
    assert body["serve_agent_runner"] is False
    # meiva:note
    # Retired updater projections are absent from the live status wire.
    assert "current_orchestration" not in body
    assert "last_updater_outcome" not in body


# meiva:note
# The retired update and recovery routes do not exist over HTTP.
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/cluster/update"),
        ("POST", "/api/cluster/rollout"),
        ("POST", "/api/cluster/restart"),
        ("GET", "/api/cluster/update-check"),
        ("POST", "/api/cluster/recover"),
    ],
)
def test_removed_ingress_cannot_dispatch(method: str, path: str) -> None:
    with gateway_test_client() as client:
        assert client.request(method, path).status_code == 404
    assert path not in openapi_schema()["paths"]


# meiva:note
# The legacy lease-bound stop/resume routes and their ops kinds no longer exist.
@pytest.mark.parametrize("route", ["/api/cluster/stop", "/api/cluster/resume"])
def test_the_legacy_deploy_lease_transition_is_gone(route: str) -> None:
    with gateway_test_client() as client:
        r = client.post(
            route,
            json={"deploy_holder": "g:pid1", "deploy_acquired_at": "2026-08-25T00:00:00Z"},
        )
    assert r.status_code in {404, 405}


# meiva:note
# `POST /api/cluster/stopping?machine=<name>&home=<home>` retracts that unit: its row is stamped
# stopped and the host's maintenance window is written (or cleared by a stop without one).
def test_post_stopping_marks_machine(database: Database, db_conn: psycopg.Connection) -> None:
    for name in ("runner-a", "runner-b"):
        register_self(
            database,
            url=f"http://{name}.invalid:9000",
            name=name,
            roles=frozenset({"agent-runner"}),
            description=None,
            home="~/.meiva",
            gateway=None,
        )

    def stopped(name: str) -> tuple[bool, str | None, float | None]:
        row = db_conn.execute(
            "SELECT u.stopped_at IS NOT NULL, m.maintenance_reason, "
            "EXTRACT(EPOCH FROM m.maintenance_until - now()) "
            "FROM machine_units u JOIN machines m ON m.name = u.machine_name "
            "WHERE u.machine_name = %s AND u.home = '~/.meiva'",
            (name,),
        ).fetchone()
        db_conn.commit()
        assert row is not None
        return row[0], row[1], None if row[2] is None else float(row[2])

    assert stopped("runner-a") == (False, None, None)
    with gateway_test_client() as client:
        r = client.post("/api/cluster/stopping", params={"machine": "runner-a", "home": "~/.meiva"})
        assert r.status_code == 200
        assert r.json() == {"machine": "runner-a"}
        assert stopped("runner-a") == (True, None, None)
        assert stopped("runner-b") == (False, None, None)

        windowed = client.post(
            "/api/cluster/stopping",
            params={
                "machine": "runner-b",
                "home": "~/.meiva",
                "maintenance_for_seconds": 2700,
                "maintenance_reason": "pgbouncer audit",
            },
        )
    assert windowed.status_code == 200
    was_stopped, reason, remaining = stopped("runner-b")
    assert was_stopped
    assert reason == "pgbouncer audit"
    assert remaining is not None and 2600 < remaining <= 2700
