from pathlib import Path

import psycopg

from base.packages.extensions import plugin_enable, registry
from base.packages.plugins import enable_config
from gateway.tests.auth.gateway_config import gateway_test_client


def test_cluster_toggle_without_online_runners(db_conn: psycopg.Connection, tmp_path: Path) -> None:
    with gateway_test_client() as client:
        response = client.put("/api/plugins", json={"name": "meiva-code", "enabled": False})
        assert response.status_code == 200, response.text
        assert response.json() == {"name": "meiva_code", "enabled": False, "restart_required": True}
        plugins = {p["name"]: p for p in client.get("/api/plugins").json()["plugins"]}
        assert plugins["meiva_code"]["enabled"] is False
    db_conn.commit()
    assert registry.get(db_conn, "meiva_code").default_enabled is False
    plugin_enable.materialize(db_conn, home=tmp_path)
    assert not enable_config.load({"meiva_code"}, home=tmp_path).plugins["meiva_code"].enabled


def test_unknown_and_invalid_writes_do_not_mutate_policy(db_conn: psycopg.Connection) -> None:
    with gateway_test_client() as client:
        db_conn.commit()
        before = plugin_enable.policy(db_conn)
        assert (
            client.put(
                "/api/plugins", json={"name": "no_such_plugin", "enabled": False}
            ).status_code
            == 400
        )
        assert (
            client.put("/api/plugins", json={"name": "meiva_code", "enabled": "false"}).status_code
            == 422
        )
    assert plugin_enable.policy(db_conn) == before
