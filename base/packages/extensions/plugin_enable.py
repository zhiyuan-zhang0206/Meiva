# meiva:link base/packages/plugins/docs/plugin-config/enable-state.meiva.okf.md

from pathlib import Path

import psycopg

from base.native_process.os_platform import file_lock
from base.packages.extensions import registry
from base.packages.plugins import enable_config


def policy(conn: psycopg.Connection) -> dict[str, bool]:
    return {ext.name: ext.default_enabled for ext in registry.list_all(conn, kind="plugin")}


def materialize(conn: psycopg.Connection, *, home: Path) -> None:
    with file_lock(home / "plugin_enable_policy.lock", timeout_s=10):
        enable_config.write_cluster_policy(policy(conn), home=home)


def initialize(conn: psycopg.Connection, *, home: Path) -> None:
    discovered = enable_config.discover_plugins(home=home)
    local = enable_config.load_for_runtime(set(discovered), home=home)
    for name in sorted(discovered):
        if registry.get(conn, name) is None:
            set_enabled(conn, name, enabled=local.plugins[name].enabled, home=home)
    materialize(conn, home=home)


def set_enabled(conn: psycopg.Connection, name: str, *, enabled: bool, home: Path) -> str:
    known = enable_config.discover_plugins(home=home)
    from base.packages.skills.names import find

    registered = {ext.name for ext in registry.list_all(conn, kind="plugin")}
    resolved = find(name, set(known) | registered)
    if resolved is None:
        raise ValueError(f"unknown plugin {name!r}")
    existing = registry.get(conn, resolved)
    if existing is not None and existing.kind != "plugin":
        raise ValueError(f"extension {resolved!r} is not a plugin")
    if existing is None:
        root = known[resolved]
        from base import paths

        builtin = root.parent.resolve() == paths.repo_plugins_dir().resolve()
        digest = None
        if not builtin:
            from base.packages.extensions.install_registry import tree_hash

            digest = tree_hash(root)
            registry.put_blob(conn, registry.pack_tree(root), name=resolved, content_hash=digest)
        registry.upsert(
            conn,
            name=resolved,
            kind="plugin",
            source="repo" if builtin else "local:gateway",
            content_hash=digest,
            trust="builtin" if builtin else "unreviewed",
        )
    registry.set_default_enabled(conn, resolved, enabled=enabled)
    return resolved
