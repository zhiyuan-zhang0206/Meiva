from __future__ import annotations

from pathlib import Path

import psycopg
from fastapi.testclient import TestClient

from base.sessions.pty.tests.pty_service import pty_service as pty_service
from gateway.tests.schedules.api_support import schedule_client


def _create(client: TestClient, **kw: object):
    body = {
        "name": "s",
        "trigger_kind": "every",
        "interval_seconds": 60,
        "script": "print(1)\n",
        **kw,
    }
    return client.post("/api/schedules", json=body)


def _versions(conn: psycopg.Connection, name: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT v.note FROM schedule_versions v JOIN schedules s ON s.id = v.schedule_id "
            "WHERE s.name = %s ORDER BY v.id",
            (name,),
        )
        return [r[0] for r in cur.fetchall()]


class TestCreate:
    def test_create_returns_full_view_and_initial_version(
        self, db_conn: psycopg.Connection
    ) -> None:
        with schedule_client() as client:
            r = _create(client, name="daily", script="print(1)\n", description="hi")
        assert r.status_code == 201
        body = r.json()
        assert body["name"] == "daily"
        assert body["enabled"] is True
        assert body["status"] == "scheduled"
        assert body["command"] == "python schedule.py"
        assert body["trigger_kind"] == "every"
        assert body["interval_seconds"] == 60
        assert body["next_launch_at"] is not None
        assert body["script"] == "print(1)\n"
        assert body["description"] == "hi"
        assert _versions(db_conn, "daily") == ["initial"]

    def test_create_bad_script_400(self, db_conn: psycopg.Connection) -> None:
        with schedule_client() as client:
            r = _create(client, name="bad", script="def (:\n")
        assert r.status_code == 400
        assert "syntax error" in r.json()["detail"]

    def test_create_duplicate_name_409(self, db_conn: psycopg.Connection) -> None:
        with schedule_client() as client:
            assert _create(client, name="dup").status_code == 201
            r = _create(client, name="dup")
        assert r.status_code == 409


class TestReadUpdateDelete:
    def test_get_missing_404(self, db_conn: psycopg.Connection) -> None:
        with schedule_client() as client:
            assert client.get("/api/schedules/9999").status_code == 404

    def test_list_omits_script(self, db_conn: psycopg.Connection) -> None:
        with schedule_client() as client:
            _create(client, name="a")
            rows = client.get("/api/schedules").json()
        assert [r["name"] for r in rows] == ["a"]
        assert "script" not in rows[0]
        assert rows[0]["enabled"] is True

    def test_put_description_only_writes_no_new_version(self, db_conn: psycopg.Connection) -> None:
        with schedule_client() as client:
            sid = _create(client, name="e").json()["id"]
            r = client.put(f"/api/schedules/{sid}", json={"description": "changed"})
        assert r.status_code == 200
        assert r.json()["description"] == "changed"
        assert _versions(db_conn, "e") == ["initial"]

    def test_put_invalid_script_400(self, db_conn: psycopg.Connection) -> None:
        with schedule_client() as client:
            sid = _create(client, name="e").json()["id"]
            r = client.put(f"/api/schedules/{sid}", json={"script": "def (:\n"})
        assert r.status_code == 400

    def test_put_script_writes_edit_version(self, db_conn: psycopg.Connection) -> None:
        with schedule_client() as client:
            sid = _create(client, name="e").json()["id"]
            r = client.put(f"/api/schedules/{sid}", json={"script": "print(2)\n"})
        assert r.status_code == 200
        assert r.json()["script"] == "print(2)\n"
        assert _versions(db_conn, "e") == ["initial", "edit"]

    def test_put_enabled_change_stops_without_a_pty_sync_request(
        self, db_conn: psycopg.Connection
    ) -> None:
        with schedule_client() as client:
            sid = _create(client, name="e").json()["id"]
            stopped = client.put(f"/api/schedules/{sid}", json={"enabled": False})

        assert stopped.status_code == 200
        assert stopped.json()["enabled"] is False
        assert stopped.json()["status"] == "stopped"
        assert stopped.json()["next_launch_at"] is None
        assert db_conn.execute("SELECT schedule_id FROM schedule_sync_requests").fetchall() == []

    def test_put_same_enabled_value_does_not_sync(self, db_conn: psycopg.Connection) -> None:
        with schedule_client() as client:
            sid = _create(client, name="e").json()["id"]
            enabled = client.put(f"/api/schedules/{sid}", json={"enabled": True})

        assert enabled.status_code == 200
        assert enabled.json()["enabled"] is True
        assert db_conn.execute("SELECT schedule_id FROM schedule_sync_requests").fetchall() == []

    def test_put_no_fields_400(self, db_conn: psycopg.Connection) -> None:
        with schedule_client() as client:
            sid = _create(client, name="e").json()["id"]
            assert client.put(f"/api/schedules/{sid}", json={}).status_code == 400

    def test_delete_then_404(self, db_conn: psycopg.Connection) -> None:
        with schedule_client() as client:
            sid = _create(client, name="d").json()["id"]
            assert client.delete(f"/api/schedules/{sid}").status_code == 200
            assert client.get(f"/api/schedules/{sid}").status_code == 404
            assert client.delete(f"/api/schedules/{sid}").status_code == 404

    def test_delete_waits_for_active_run(self, db_conn: psycopg.Connection) -> None:
        with schedule_client() as client:
            sid = _create(client, name="running-delete").json()["id"]
            db_conn.execute("INSERT INTO schedule_runs (schedule_id) VALUES (%s)", (sid,))
            db_conn.commit()

            response = client.delete(f"/api/schedules/{sid}")

        assert response.status_code == 409
        assert response.json()["detail"] == "schedule is running; wait for it to finish"
        assert db_conn.execute("SELECT 1 FROM schedules WHERE id = %s", (sid,)).fetchone()


class TestSyncRequests:
    def test_control_never_writes_a_pty_sync_request(self, db_conn: psycopg.Connection) -> None:
        with schedule_client() as client:
            sid = _create(client, name="q", enabled=False).json()["id"]
            client.post(f"/api/schedules/{sid}/start")
            client.post(f"/api/schedules/{sid}/stop")
            assert (
                db_conn.execute("SELECT schedule_id FROM schedule_sync_requests").fetchall() == []
            )
            assert client.delete(f"/api/schedules/{sid}").status_code == 200
            assert (
                db_conn.execute("SELECT schedule_id FROM schedule_sync_requests").fetchall() == []
            )


class TestControl:
    def test_start_toggles_enabled_and_sets_next_launch(self, db_conn: psycopg.Connection) -> None:
        with schedule_client() as client:
            sid = _create(client, name="c", enabled=False).json()["id"]
            started = client.post(f"/api/schedules/{sid}/start").json()
        assert started["enabled"] is True
        assert started["status"] == "scheduled"
        assert started["next_launch_at"] is not None
        assert db_conn.execute("SELECT schedule_id FROM schedule_sync_requests").fetchall() == []

    def test_stop_sets_stopped_without_pty_convergence(self, db_conn: psycopg.Connection) -> None:
        with schedule_client() as client:
            sid = _create(client, name="c").json()["id"]
            stopped = client.post(f"/api/schedules/{sid}/stop").json()
        assert stopped["enabled"] is False
        assert stopped["status"] == "stopped"
        assert db_conn.execute("SELECT schedule_id FROM schedule_sync_requests").fetchall() == []

    def test_restart_disabled_409(self, db_conn: psycopg.Connection) -> None:
        with schedule_client() as client:
            sid = _create(client, name="c", enabled=False).json()["id"]
            r = client.post(f"/api/schedules/{sid}/restart")
        assert r.status_code == 409

    def test_restart_enabled_ok(self, db_conn: psycopg.Connection) -> None:
        with schedule_client() as client:
            sid = _create(client, name="c", enabled=True).json()["id"]
            assert client.post(f"/api/schedules/{sid}/restart").status_code == 200


class TestLogsRuns:
    def test_logs_falls_back_to_last_error(self, db_conn: psycopg.Connection) -> None:
        with schedule_client() as client:
            sid = _create(client, name="l").json()["id"]
            with db_conn.cursor() as cur:
                cur.execute(
                    "UPDATE schedules SET last_error = %s WHERE id = %s", ("boom\ntrace", sid)
                )
            db_conn.commit()
            body = client.get(f"/api/schedules/{sid}/logs").json()
        assert body["source"] == "last_error"
        assert body["lines"] == ["boom", "trace"]

    def test_logs_reads_latest_declarative_output(
        self,
        db_conn: psycopg.Connection,
        unit_home: Path,
    ) -> None:
        with schedule_client() as client:
            sid = _create(client, name="l").json()["id"]
            log = unit_home / "schedules" / str(sid) / "latest.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text("line one\nline two\n")
            body = client.get(f"/api/schedules/{sid}/logs?lines=3").json()
        assert body["source"] == "latest"
        assert body["lines"] == ["line one", "line two"]

    def test_logs_none_when_no_output(self, db_conn: psycopg.Connection) -> None:
        with schedule_client() as client:
            sid = _create(client, name="l").json()["id"]
            body = client.get(f"/api/schedules/{sid}/logs").json()
        assert body["source"] == "none"
        assert body["lines"] == []

    def test_runs_empty(self, db_conn: psycopg.Connection) -> None:
        with schedule_client() as client:
            sid = _create(client, name="r").json()["id"]
            assert client.get(f"/api/schedules/{sid}/runs").json() == []

    def test_runs_default_limit_comes_from_display_config(
        self, db_conn: psycopg.Connection
    ) -> None:
        with schedule_client(fields={"schedules_runs_default_limit": 1}) as client:
            sid = _create(client, name="r").json()["id"]
            with db_conn.cursor() as cur:
                cur.execute("INSERT INTO schedule_runs (schedule_id) VALUES (%s)", (sid,))
                cur.execute("INSERT INTO schedule_runs (schedule_id) VALUES (%s)", (sid,))
            db_conn.commit()
            body = client.get(f"/api/schedules/{sid}/runs").json()
        assert len(body) == 1

    def test_runs_missing_schedule_404(self, db_conn: psycopg.Connection) -> None:
        with schedule_client() as client:
            assert client.get("/api/schedules/9999/runs").status_code == 404

    def test_runs_shows_in_progress_null_row(self, db_conn: psycopg.Connection) -> None:
        with schedule_client() as client:
            sid = _create(client, name="r2").json()["id"]
            with db_conn.cursor() as cur:
                cur.execute("INSERT INTO schedule_runs (schedule_id) VALUES (%s)", (sid,))
            db_conn.commit()
            body = client.get(f"/api/schedules/{sid}/runs").json()
        assert len(body) == 1
        assert body[0]["ok"] is None
        assert body[0]["note"] is None

    def test_runs_orders_newest_first_with_id_tiebreak(self, db_conn: psycopg.Connection) -> None:
        with schedule_client() as client:
            sid = _create(client, name="r3").json()["id"]
            with db_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO schedule_runs (schedule_id, ran_at) "
                    "VALUES (%s, now() - interval '1 hour') RETURNING id",
                    (sid,),
                )
                row = cur.fetchone()
                assert row is not None
                rid_old = row[0]
                cur.execute(
                    "INSERT INTO schedule_runs (schedule_id, ran_at) "
                    "VALUES (%s, now() - interval '1 hour') RETURNING id",
                    (sid,),
                )
                row = cur.fetchone()
                assert row is not None
                rid_new = row[0]
            db_conn.commit()
            body = client.get(f"/api/schedules/{sid}/runs").json()
        assert [r["id"] for r in body] == [rid_new, rid_old]
