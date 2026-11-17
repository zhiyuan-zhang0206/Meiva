from datetime import UTC, datetime

import psycopg
import pytest

import base.db
from base.api_contracts.idempotency import keyed_headers
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db.code_version_gate import ProcessDbGate
from gateway.schedules import router
from gateway.schedules.tests.db_faults import failing_write
from gateway.tests.schedules.api_support import schedule_client

_BODY = {
    "trigger_kind": "every",
    "interval_seconds": 60,
    "script": "pass",
}


def test_failed_receipt_commit_rolls_back_restart_revision_and_queue(
    gateway_data_plane: None,
    db_conn: psycopg.Connection,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    with schedule_client(machine=machine_identity) as client:
        sid = client.post("/api/schedules", json={"name": "rollback", **_BODY}).json()["id"]

    with (
        failing_write(db_conn, table="schedule_operation_receipts", events="UPDATE"),
        base.db.pool(gate=database_gate) as pool,
        pytest.raises(psycopg.errors.RaiseException, match="injected fault"),
    ):
        router.control_blocking(pool, sid, "run_now", "run-now", datetime(2026, 11, 17, tzinfo=UTC))
    assert db_conn.execute("SELECT status FROM schedules WHERE id = %s", (sid,)).fetchone() == (
        "scheduled",
    )
    assert db_conn.execute("SELECT count(*) FROM schedule_operation_receipts").fetchone() == (0,)
    assert db_conn.execute("SELECT count(*) FROM schedule_sync_requests").fetchone() == (0,)


def test_receipt_replays_after_schedule_deletion(
    gateway_data_plane: None,
    db_conn: psycopg.Connection,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    with schedule_client(machine=machine_identity) as client:
        sid = client.post("/api/schedules", json={"name": "deleted", **_BODY}).json()["id"]
        path = f"/api/schedules/{sid}/run-now"
        headers = keyed_headers("before-delete")
        original = client.post(path, headers=headers)
        assert client.delete(f"/api/schedules/{sid}").status_code == 200
        assert client.post(path, headers=headers).json() == original.json()
        assert client.post(path, headers=keyed_headers("after-delete")).status_code == 404
    assert db_conn.execute("SELECT id FROM schedules WHERE id = %s", (sid,)).fetchone() is None


def test_delete_cleanup_enqueue_failure_preserves_schedule(
    gateway_data_plane: None,
    db_conn: psycopg.Connection,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    with schedule_client(machine=machine_identity) as client:
        sid = client.post("/api/schedules", json={"name": "delete-rollback", **_BODY}).json()["id"]

    with (
        failing_write(db_conn, table="schedules", events="DELETE"),
        base.db.pool(gate=database_gate) as pool,
        pytest.raises(psycopg.errors.RaiseException, match="injected fault"),
    ):
        router.delete_blocking(pool, sid)
    assert db_conn.execute("SELECT id FROM schedules WHERE id = %s", (sid,)).fetchone() == (sid,)


def test_at_creation_receipt_replays_and_rejects_mismatch(
    gateway_data_plane: None,
    machine_identity: MachineIdentityReader,
) -> None:
    body = {
        "name": "idempotent-at",
        "trigger_kind": "at",
        "run_at": "2030-01-01T00:00:00Z",
        "script": "pass",
    }
    headers = keyed_headers("create-at")
    with schedule_client(machine=machine_identity) as client:
        original = client.post("/api/schedules", json=body, headers=headers)
        assert original.status_code == 201
        replay = client.post("/api/schedules", json=body, headers=headers)
        assert replay.status_code == 201
        assert replay.json()["id"] == original.json()["id"]
        mismatch = client.post(
            "/api/schedules", json={**body, "run_at": "2030-01-02T00:00:00Z"}, headers=headers
        )
        assert mismatch.status_code == 409
