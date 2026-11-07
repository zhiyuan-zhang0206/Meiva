from pathlib import Path

import psycopg
import pytest

from base.packages.extensions import plugin_enable, registry
from base.packages.plugins import enable_config


def test_returning_and_new_homes_obey_policy(db_conn: psycopg.Connection, tmp_path: Path) -> None:
    registry.upsert(db_conn, name="meiva_code", kind="plugin", source="repo", content_hash=None)
    registry.set_default_enabled(db_conn, "meiva_code", enabled=False)
    homes = [tmp_path / "online", tmp_path / "offline", tmp_path / "new"]
    for home in homes[:2]:
        home.mkdir()
        enable_config.write_local({"plugins": {"meiva_code": {"enabled": True}}}, home=home)
    plugin_enable.materialize(db_conn, home=homes[0])
    assert not enable_config.load({"meiva_code"}, home=homes[0]).plugins["meiva_code"].enabled
    for home in homes[1:]:
        plugin_enable.materialize(db_conn, home=home)
        assert not enable_config.load({"meiva_code"}, home=home).plugins["meiva_code"].enabled
    with pytest.raises(enable_config.PluginsConfigError, match="cluster-managed"):
        enable_config.PluginEnableStore(homes[0] / "plugins_config.json").set_enabled(
            "meiva_code", enabled=True, known={"meiva_code"}
        )
    registry.set_default_enabled(db_conn, "meiva_code", enabled=True)
    plugin_enable.materialize(db_conn, home=homes[0])
    assert enable_config.load({"meiva_code"}, home=homes[0]).plugins["meiva_code"].enabled


def test_disabled_policy_survives_upgrade_and_absent_local_content(
    db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    registry.upsert(
        db_conn,
        name="future_plugin",
        kind="plugin",
        source="repo",
        content_hash=None,
        default_enabled=False,
    )
    registry.upsert(db_conn, name="future_plugin", kind="plugin", source="repo", content_hash=None)
    plugin_enable.materialize(db_conn, home=tmp_path)
    assert enable_config.load(set(), home=tmp_path).plugins == {}
    assert not enable_config.load({"future_plugin"}, home=tmp_path).plugins["future_plugin"].enabled


def test_malformed_cache_is_not_replaced_with_all_enabled(tmp_path: Path) -> None:
    (tmp_path / "plugin_enable_policy.json").write_text('{"meiva_code": "false"}')
    with pytest.raises(ValueError):
        enable_config.load({"meiva_code"}, home=tmp_path)
