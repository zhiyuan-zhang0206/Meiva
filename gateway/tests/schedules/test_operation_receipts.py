from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

import base.db
from base.api_contracts.idempotency import keyed_headers
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db.code_version_gate import ProcessDbGate
from gateway.schedules import router
from gateway.tests.schedules.api_support import schedule_client


def _create(client: TestClient) -> int:
    return client.post(
        "/api/schedules",
        json={
            "name": "receipt",
            "trigger_kind": "every",
            "interval_seconds": 60,
            "script": "pass",
        },
    ).json()["id"]


def test_concurrent_run_now_uses_one_operation_receipt(
    gateway_data_plane: None,
    db_conn: psycopg.Connection,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    with schedule_client(machine=machine_identity) as client:
        sid = _create(client)
    with (
        base.db.pool(max_size=4, gate=database_gate) as pool,
        ThreadPoolExecutor(max_workers=4) as workers,
    ):

        def run_now(_worker: int) -> tuple[tuple[Any, ...], bool]:
            return router.control_blocking(
                pool, sid, "run_now", "one-run-now", datetime(2026, 11, 17, tzinfo=UTC)
            )

        results = list(workers.map(run_now, range(4)))
    assert sum(changed for _, changed in results) == 1
    assert db_conn.execute(
        "SELECT status, enabled FROM schedules WHERE id = %s", (sid,)
    ).fetchone() == ("scheduled", True)
    assert db_conn.execute("SELECT count(*) FROM schedule_operation_receipts").fetchone() == (1,)


def test_delayed_run_now_replay_cannot_undo_newer_disable(
    gateway_data_plane: None,
    db_conn: psycopg.Connection,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    with schedule_client(machine=machine_identity) as client:
        sid = _create(client)
        path = f"/api/schedules/{sid}/run-now"
        headers = keyed_headers("run-now-one")
        original = client.post(path, headers=headers)
        assert original.status_code == 200
        assert client.post(f"/api/schedules/{sid}/disable").status_code == 200
        assert client.post(path, headers=headers).json() == original.json()
        assert client.get(f"/api/schedules/{sid}").json()["enabled"] is False
        assert client.post(path, headers=keyed_headers("run-now-two")).status_code == 200
    assert db_conn.execute(
        "SELECT enabled, status FROM schedules WHERE id = %s", (sid,)
    ).fetchone() == (True, "scheduled")


def test_edit_receipt_replays_original_result_and_conflicts_on_changed_payload(
    gateway_data_plane: None,
    db_conn: psycopg.Connection,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    with schedule_client(machine=machine_identity) as client:
        sid = _create(client)
        path = f"/api/schedules/{sid}"
        headers = keyed_headers("edit-one")
        original = client.put(path, json={"script": "print(1)"}, headers=headers)
        assert original.status_code == 200
        assert client.put(path, json={"script": "print(2)"}).status_code == 200
        assert (
            client.put(path, json={"script": "print(1)"}, headers=headers).json() == original.json()
        )
        assert client.put(path, json={"script": "print(3)"}, headers=headers).status_code == 409
    assert db_conn.execute(
        "SELECT script, desired_revision FROM schedules WHERE id = %s", (sid,)
    ).fetchone() == ("print(2)", 0)


@pytest.mark.parametrize(
    "headers",
    [
        {"Idempotency-Key": ""},
        {"Idempotency-Key": "x" * 129},
        {"Idempotency-Scope": "principal-v1"},
        {"Idempotency-Key": "x", "Idempotency-Scope": "unknown"},
        {"Idempotency-Key": "unscoped"},
    ],
)
def test_malformed_operation_identity_does_not_change_schedule(
    gateway_data_plane: None,
    db_conn: psycopg.Connection,
    headers: dict[str, str],
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    with schedule_client(machine=machine_identity) as client:
        sid = _create(client)
        assert client.post(f"/api/schedules/{sid}/run-now", headers=headers).status_code == 400
    assert db_conn.execute(
        "SELECT desired_revision FROM schedules WHERE id = %s", (sid,)
    ).fetchone() == (0,)


def test_new_run_now_identity_is_a_new_deliberate_rerun(
    gateway_data_plane: None,
    db_conn: psycopg.Connection,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    with schedule_client(machine=machine_identity) as client:
        sid = _create(client)
        for key in ("one", "two"):
            assert (
                client.post(f"/api/schedules/{sid}/run-now", headers=keyed_headers(key)).status_code
                == 200
            )
    assert db_conn.execute("SELECT count(*) FROM schedule_operation_receipts").fetchone() == (2,)
