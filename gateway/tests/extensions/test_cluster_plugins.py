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
    stored = registry.get(db_conn, "meiva_code")
    assert stored is not None
    assert stored.default_enabled is False
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


def test_provider_metadata_and_crafted_toggle_rejection(db_conn: psycopg.Connection) -> None:
    with gateway_test_client() as client:
        plugins = {p["name"]: p for p in client.get("/api/plugins").json()["plugins"]}
        assert plugins["lm_anthropic"]["provider_kinds"] == ["llm"]
        assert plugins["lm_anthropic"]["display_name"] == "Anthropic"
        assert plugins["lm_typesafe"]["provider_kinds"] == ["decision"]
        assert plugins["lm_openai"]["provider_kinds"] == ["llm", "decision"]
        assert plugins["meiva_code"]["user_toggleable"] is True
        db_conn.commit()
        before = plugin_enable.policy(db_conn)
        for name in ("lm_anthropic", "lm-anthropic", "lm_typesafe"):
            response = client.put("/api/plugins", json={"name": name, "enabled": False})
            assert response.status_code == 403, response.text
        assert plugin_enable.policy(db_conn) == before


def test_registry_only_readonly_plugin_keeps_policy_and_category(
    db_conn: psycopg.Connection,
) -> None:
    registry.upsert(
        db_conn,
        name="remote_readonly",
        kind="plugin",
        source="repo",
        content_hash=None,
        default_enabled=False,
        manifest={
            "apiVersion": 2,
            "name": "remote_readonly",
            "version": "1.0.0",
            "engines": {"meiva": ">=2026.10.1"},
            "displayName": "Remote capability",
            "category": "builtin",
            "userToggleable": False,
        },
    )
    db_conn.commit()
    with gateway_test_client() as client:
        plugins = {p["name"]: p for p in client.get("/api/plugins").json()["plugins"]}
        assert plugins["remote_readonly"] == {
            "name": "remote_readonly",
            "enabled": False,
            "description": "",
            "display_name": "Remote capability",
            "user_toggleable": False,
            "category": "builtin",
            "provider_kinds": [],
        }
        assert plugins["meiva_code"]["category"] == "builtin"
        response = client.put("/api/plugins", json={"name": "remote_readonly", "enabled": True})
        assert response.status_code == 403
    stored = registry.get(db_conn, "remote_readonly")
    assert stored is not None
    assert stored.default_enabled is False
    assert stored.trust == "unreviewed"
