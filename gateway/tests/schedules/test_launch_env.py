from __future__ import annotations

import psycopg

from gateway.tests.schedules.api_support import schedule_client


def test_launch_env_requires_cluster_credential(db_conn: psycopg.Connection) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schedules (name, script, command, enabled, machine, secret_env_vars) "
            "VALUES ('sec-test-1', 'print(1)\\n', 'python script.py', true, 'gateway', ARRAY['SECRET_A']) "
            "RETURNING id"
        )
        row = cur.fetchone()
        assert row is not None
        sched_id = row[0]
    db_conn.commit()

    with schedule_client(credentials=False) as client:
        r = client.get(f"/api/schedules/{sched_id}/launch-env")
        assert r.status_code == 401
        assert r.json()["detail"] in ("machine API token required", "authentication required")

        r_bad = client.get(
            f"/api/schedules/{sched_id}/launch-env",
            headers={"Authorization": "Bearer invalid-token"},
        )
        assert r_bad.status_code == 401
        assert r_bad.json()["detail"] in ("machine API token required", "authentication required")


def test_launch_env_serves_declared_secrets(db_conn: psycopg.Connection) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schedules (name, script, command, enabled, machine, secret_env_vars) "
            "VALUES ('sec-test-2', 'print(1)\\n', 'python script.py', true, 'gateway', ARRAY['API_TOKEN', 'MEIVA_CLUSTER_SECRET', 'EMPTY_VAR']) "
            "RETURNING id"
        )
        row = cur.fetchone()
        assert row is not None
        sched_id = row[0]
    db_conn.commit()

    with schedule_client() as client:
        secret = client.app.state.gateway_config.cluster_secret
        headers = {"Authorization": f"Bearer {secret}"}

        class FakeAuthority:
            def read_env_aliases(self) -> dict[str, str]:
                return {
                    "API_TOKEN": "token-12345",
                    "MEIVA_CLUSTER_SECRET": "should-be-omitted",
                    "OTHER_SECRET": "not-requested",
                }

        client.app.state.config_authority = FakeAuthority()

        resp = client.get(f"/api/schedules/{sched_id}/launch-env", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data == {"API_TOKEN": "token-12345"}
        assert "MEIVA_CLUSTER_SECRET" not in data
        assert "OTHER_SECRET" not in data


def test_launch_env_disabled_schedule_returns_empty(db_conn: psycopg.Connection) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schedules (name, script, command, enabled, machine, secret_env_vars) "
            "VALUES ('sec-test-3', 'print(1)\\n', 'python script.py', false, 'gateway', ARRAY['API_TOKEN']) "
            "RETURNING id"
        )
        row = cur.fetchone()
        assert row is not None
        sched_id = row[0]
    db_conn.commit()

    with schedule_client() as client:
        secret = client.app.state.gateway_config.cluster_secret
        headers = {"Authorization": f"Bearer {secret}"}

        resp = client.get(f"/api/schedules/{sched_id}/launch-env", headers=headers)
        assert resp.status_code == 200
        assert resp.json() == {}


def test_launch_env_missing_schedule_returns_404(db_conn: psycopg.Connection) -> None:
    with schedule_client() as client:
        secret = client.app.state.gateway_config.cluster_secret
        headers = {"Authorization": f"Bearer {secret}"}

        resp = client.get("/api/schedules/9999999/launch-env", headers=headers)
        assert resp.status_code == 404
