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


def test_initialize_clears_retired_builtin_rows_from_registry_and_cache(
    db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    retired = sorted(enable_config.RETIRED_BUILTIN_PLUGINS)
    for name in retired:
        registry.upsert(db_conn, name=name, kind="plugin", source="repo", content_hash=None)
    registry.upsert(db_conn, name="future_plugin", kind="plugin", source="repo", content_hash=None)
    plugin_enable.materialize(db_conn, home=tmp_path)
    assert set(retired) <= enable_config.read_cluster_policy(home=tmp_path).keys()

    plugin_enable.initialize(db_conn, home=tmp_path)

    policy = enable_config.read_cluster_policy(home=tmp_path)
    assert not set(retired) & policy.keys()
    assert not set(retired) & {ext.name for ext in registry.list_all(db_conn, kind="plugin")}
    assert "future_plugin" in policy


def test_retiring_a_builtin_name_keeps_an_installed_extension_of_that_name(
    db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    name = sorted(enable_config.RETIRED_BUILTIN_PLUGINS)[0]
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "plugin.py").write_text("")
    archive = registry.pack_tree(tree)
    digest = registry.blob_hash(archive)
    registry.put_blob(db_conn, archive, name=name, content_hash=digest)
    registry.upsert(db_conn, name=name, kind="plugin", source="local:gateway", content_hash=digest)

    assert plugin_enable.retire_builtin_rows(db_conn) == []
    assert registry.get(db_conn, name) is not None
