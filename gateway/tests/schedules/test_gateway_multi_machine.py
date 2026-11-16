from __future__ import annotations

import psycopg

from gateway.tests.schedules.api_support import schedule_client


def test_schedule_placement_in_api(db_conn: psycopg.Connection) -> None:
    with schedule_client() as client:
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO machines (name) VALUES ('test-box') ON CONFLICT (name) DO NOTHING"
            )
            cur.execute(
                "INSERT INTO machine_probe (machine_name, online, consecutive_failures) "
                "VALUES ('test-box', true, 0) "
                "ON CONFLICT (machine_name) DO UPDATE SET online = true, consecutive_failures = 0"
            )
        db_conn.commit()

        resp = client.post(
            "/api/schedules",
            json={
                "name": "placed-task",
                "trigger_kind": "every",
                "interval_seconds": 60,
                "script": "print('ok')\n",
                "command": "python schedule.py",
                "machine": "test-box",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["machine"] == "test-box"
        assert data["placement_status"] == "ok"
        assert data["placement_reason"] is None

        resp2 = client.post(
            "/api/schedules",
            json={
                "name": "unplaced-task",
                "trigger_kind": "every",
                "interval_seconds": 60,
                "script": "print('ok')\n",
                "command": "python schedule.py",
                "machine": "missing-box",
            },
        )
        assert resp2.status_code == 201
        data2 = resp2.json()
        assert data2["machine"] == "missing-box"
        assert data2["placement_status"] == "unregistered"
        assert "not registered" in data2["placement_reason"]


def test_declarative_schedule_machine_move_avoids_pty_sync_requests(
    db_conn: psycopg.Connection,
) -> None:
    with schedule_client() as client:
        created = client.post(
            "/api/schedules",
            json={
                "name": "moving-task",
                "trigger_kind": "every",
                "interval_seconds": 60,
                "script": "print('move')\n",
                "command": "python schedule.py",
                "machine": "box-a",
            },
        ).json()
        sid = created["id"]

        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM schedule_sync_requests WHERE schedule_id = %s", (sid,))
        db_conn.commit()

        updated = client.put(f"/api/schedules/{sid}", json={"machine": "box-b"})
        assert updated.status_code == 200
        view = updated.json()
        assert view["machine"] == "box-b"

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT launch_generation, launch_count FROM schedules WHERE id = %s", (sid,)
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] is None
            assert row[1] == 0

            cur.execute(
                "SELECT machine FROM schedule_sync_requests WHERE schedule_id = %s ORDER BY machine",
                (sid,),
            )
            queued_machines = [r[0] for r in cur.fetchall()]
        assert queued_machines == []


def test_declarative_schedule_machine_move_waits_for_active_run(
    db_conn: psycopg.Connection,
) -> None:
    with schedule_client() as client:
        sid = client.post(
            "/api/schedules",
            json={
                "name": "running-move",
                "trigger_kind": "every",
                "interval_seconds": 60,
                "script": "print('busy')\n",
                "machine": "box-a",
            },
        ).json()["id"]
        db_conn.execute("INSERT INTO schedule_runs (schedule_id) VALUES (%s)", (sid,))
        db_conn.commit()

        response = client.put(f"/api/schedules/{sid}", json={"machine": "box-b"})

    assert response.status_code == 409
    assert response.json()["detail"] == "schedule is running; wait for it to finish"
    assert db_conn.execute("SELECT machine FROM schedules WHERE id = %s", (sid,)).fetchone() == (
        "box-a",
    )


def test_cross_machine_logs_rpc_dispatch(db_conn: psycopg.Connection) -> None:
    with schedule_client() as client:
        created = client.post(
            "/api/schedules",
            json={
                "name": "remote-task",
                "trigger_kind": "every",
                "interval_seconds": 60,
                "script": "print('remote')\n",
                "command": "python schedule.py",
                "machine": "remote-machine-unreachable",
            },
        ).json()
        sid = created["id"]

        logs_resp = client.get(f"/api/schedules/{sid}/logs")
        assert logs_resp.status_code == 503
        assert "ops server unreachable" in logs_resp.json()["detail"]
