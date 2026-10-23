import time
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from fastapi.testclient import TestClient

import base.db
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db.code_version_gate import ProcessDbGate
from gateway.schedules import router
from gateway.schedules.tests.db_faults import failing_write
from gateway.tests.schedules.api_support import schedule_client


def _create(client: TestClient) -> int:
    response = client.post("/api/schedules", json={"name": "delivery", "script": "print(1)\n"})
    assert response.status_code == 201
    return response.json()["id"]


@pytest.mark.parametrize(
    "fields",
    [
        {"script": "print(1)\n"},
        {"command": "python schedule.py"},
        {"script": "print(1)\n", "enabled": True},
    ],
)
def test_same_value_edit_preserves_version_timestamp_and_queue(
    gateway_data_plane: None,
    db_conn: psycopg.Connection,
    fields: dict[str, object],
    *,
    machine_identity: MachineIdentity,
) -> None:
    with schedule_client(machine=machine_identity) as client:
        sid = _create(client)
        before = db_conn.execute(
            "SELECT updated_at FROM schedules WHERE id = %s", (sid,)
        ).fetchone()
        response = client.put(f"/api/schedules/{sid}", json=fields)
    assert response.status_code == 200
    assert (
        db_conn.execute("SELECT updated_at FROM schedules WHERE id = %s", (sid,)).fetchone()
        == before
    )
    assert db_conn.execute(
        "SELECT note FROM schedule_versions WHERE schedule_id = %s", (sid,)
    ).fetchall() == [("initial",)]
    assert db_conn.execute("SELECT schedule_id FROM schedule_sync_requests").fetchall() == []


def test_concurrent_identical_edits_append_one_version(
    gateway_data_plane: None,
    db_conn: psycopg.Connection,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    with schedule_client(machine=machine_identity) as client:
        sid = _create(client)
    with (
        base.db.pool(max_size=4, gate=database_gate) as pool,
        ThreadPoolExecutor(max_workers=4) as workers,
    ):

        def edit(_worker: int) -> tuple[tuple[object, ...], bool]:
            return router.update_blocking(pool, sid, {"script": "print(2)\n"})

        results = list(workers.map(edit, range(4)))
    assert sum(needs_sync for _, needs_sync in results) == 1
    assert db_conn.execute(
        "SELECT note FROM schedule_versions WHERE schedule_id = %s ORDER BY id", (sid,)
    ).fetchall() == [("initial",), ("edit",)]
    assert db_conn.execute("SELECT schedule_id FROM schedule_sync_requests").fetchall() == [(sid,)]


def test_enqueue_failure_rolls_back_config_and_version(
    gateway_data_plane: None,
    db_conn: psycopg.Connection,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    with schedule_client(machine=machine_identity) as client:
        sid = _create(client)

    with (
        failing_write(db_conn, table="schedule_sync_requests", events="INSERT OR UPDATE"),
        base.db.pool(gate=database_gate) as pool,
        pytest.raises(psycopg.errors.RaiseException, match="injected fault"),
    ):
        router.update_blocking(pool, sid, {"script": "print(2)\n"})
    assert db_conn.execute("SELECT script FROM schedules WHERE id = %s", (sid,)).fetchone() == (
        "print(1)\n",
    )
    assert db_conn.execute(
        "SELECT note FROM schedule_versions WHERE schedule_id = %s", (sid,)
    ).fetchall() == [("initial",)]


def test_convergence_work_is_committed_before_the_response_waits_for_it(
    gateway_data_plane: None,
    db_conn: psycopg.Connection,
    *,
    machine_identity: MachineIdentity,
) -> None:
    with (
        schedule_client(machine=machine_identity, wait_s=3.0) as client,
        ThreadPoolExecutor(max_workers=1) as workers,
    ):
        sid = _create(client)
        response = workers.submit(
            client.put, f"/api/schedules/{sid}", json={"script": "print(2)\n"}
        )
        deadline = time.monotonic() + 5
        while not db_conn.execute("SELECT 1 FROM schedule_sync_requests").fetchall():
            db_conn.rollback()
            assert time.monotonic() < deadline, "the sync request was never queued"
            time.sleep(0.02)
        # meiva:note
        # The request is still waiting for a manager that does not exist, yet its work is durable.
        assert not response.done()
        assert db_conn.execute("SELECT script FROM schedules WHERE id = %s", (sid,)).fetchone() == (
            "print(2)\n",
        )
        assert response.result(timeout=10).status_code == 200
    assert db_conn.execute("SELECT schedule_id FROM schedule_sync_requests").fetchall() == [(sid,)]
    assert db_conn.execute(
        "SELECT note FROM schedule_versions WHERE schedule_id = %s ORDER BY id", (sid,)
    ).fetchall() == [("initial",), ("edit",)]


@pytest.mark.parametrize("enabled,action", [(True, "start"), (False, "stop")])
def test_repeated_enabled_choice_does_not_enqueue(
    gateway_data_plane: None,
    db_conn: psycopg.Connection,
    enabled: bool,
    action: str,
    *,
    machine_identity: MachineIdentity,
) -> None:
    with schedule_client(machine=machine_identity) as client:
        sid = client.post(
            "/api/schedules", json={"name": "choice", "script": "pass", "enabled": enabled}
        ).json()["id"]
        assert client.post(f"/api/schedules/{sid}/{action}").status_code == 200
        assert client.post(f"/api/schedules/{sid}/{action}").status_code == 200
    assert db_conn.execute("SELECT schedule_id FROM schedule_sync_requests").fetchall() == []


@pytest.mark.parametrize("status", ["completed", "error"])
def test_explicit_start_preserves_terminal_rerun(
    gateway_data_plane: None,
    db_conn: psycopg.Connection,
    status: str,
    *,
    machine_identity: MachineIdentity,
) -> None:
    with schedule_client(machine=machine_identity) as client:
        sid = _create(client)
        db_conn.execute("UPDATE schedules SET status = %s WHERE id = %s", (status, sid))
        db_conn.commit()
        assert client.post(f"/api/schedules/{sid}/start").status_code == 200
    assert db_conn.execute("SELECT schedule_id FROM schedule_sync_requests").fetchall() == [(sid,)]
