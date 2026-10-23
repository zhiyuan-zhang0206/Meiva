"""base/packages/plugins/enable_config.py unit tests — schema + load + validation.

Plugin config is per-machine local only: reads and writes go through
~/.meiva/plugins_config.json. No DB involvement.
"""

import json
from pathlib import Path

import pytest

from base.packages.plugins.enable_config import (
    DanglingPlugin,
    DuplicatePlugin,
    PluginEnableStore,
    PluginEntry,
    PluginsConfig,
    PluginsConfigError,
    SchemaInvalid,
    discover_plugin_dirs,
    parse_description,
)


@pytest.fixture(autouse=True)
def plugin_roots(tmp_path: Path) -> None:
    (tmp_path / "repo_plugins").mkdir()
    (tmp_path / "user_plugins").mkdir()


@pytest.fixture
def store(tmp_path: Path) -> PluginEnableStore:
    return PluginEnableStore(tmp_path / "plugins_config.json")


def _discover(tmp_path: Path) -> dict[str, Path]:
    return discover_plugin_dirs(
        repo_dir=tmp_path / "repo_plugins", user_dir=tmp_path / "user_plugins"
    )


def _make_external_plugin(name: str, tmp_path: Path) -> None:
    """Create a plugin in the external plugin directory."""
    user_root = tmp_path / "user_plugins"
    plugin_dir = user_root / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.py").write_text(f'__description__ = "{name} plugin"\n')


def _make_plugin_dir(name: str, tmp_path: Path) -> Path:
    root = tmp_path / "repo_plugins"
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.py").write_text(f'__description__ = "{name} plugin"\n')
    return plugin_dir


# ── schema ──


def test_plugin_entry_defaults():
    entry = PluginEntry(enabled=True)
    assert entry.enabled is True


def test_plugins_config_defaults():
    cfg = PluginsConfig()
    assert cfg.plugins == {}


# ── load ──


def test_load_writes_default_when_no_local_file(tmp_path: Path, store: PluginEnableStore):
    _make_plugin_dir("compact", tmp_path)
    _make_plugin_dir("syntax_fix", tmp_path)

    cfg = store.load({"compact", "syntax_fix"})
    assert set(cfg.plugins) == {"compact", "syntax_fix"}
    assert all(e.enabled for e in cfg.plugins.values())


def test_load_reads_local_file(tmp_path: Path, store: PluginEnableStore):
    # meiva:link base/packages/plugins/docs/plugin-config/enable-state.meiva.okf.md

    _make_plugin_dir("compact", tmp_path)
    store.path.write_text(json.dumps({"plugins": {"compact": {"enabled": False}}}))
    cfg = store.load({"compact"})
    assert cfg.plugins["compact"].enabled is False


def test_load_reads_existing_config(tmp_path: Path, store: PluginEnableStore):
    _make_plugin_dir("compact", tmp_path)
    store.write({"plugins": {"compact": {"enabled": False}}})

    cfg = store.load({"compact"})
    assert cfg.plugins["compact"].enabled is False


def test_load_auto_merges_new_plugins(tmp_path: Path, store: PluginEnableStore):
    _make_plugin_dir("compact", tmp_path)
    _make_plugin_dir("syntax_fix", tmp_path)
    store.write({"plugins": {"compact": {"enabled": False}}})

    cfg = store.load({"compact", "syntax_fix"})
    assert cfg.plugins["compact"].enabled is False
    assert cfg.plugins["syntax_fix"].enabled is True


def test_load_auto_merge_new_plugins_deterministic_order(tmp_path: Path, store: PluginEnableStore):
    """New in-memory entries preserve the sorted plugin-load invariant."""
    for name in ("zeta", "alpha", "mid"):
        _make_plugin_dir(name, tmp_path)
    store.write({"plugins": {"alpha": {"enabled": False}}})

    cfg = store.load({"zeta", "alpha", "mid"})

    assert list(cfg.plugins) == ["alpha", "mid", "zeta"]


def test_load_raises_on_dangling(tmp_path: Path, store: PluginEnableStore):
    _make_plugin_dir("compact", tmp_path)
    store.write({"plugins": {"ghost": {"enabled": True}}})

    with pytest.raises(DanglingPlugin, match="ghost"):
        store.load({"compact"})


# ── set_local_enabled ──


def test_set_local_enabled_writes_sorted_order(tmp_path: Path, store: PluginEnableStore):
    """The written plugins.json key order is sorted, not set-hash-dependent —
    the on-disk iteration order drives hook registration order, so it must be
    deterministic. Created out of alphabetical order on purpose."""

    for name in ("zeta", "alpha", "mid"):
        _make_plugin_dir(name, tmp_path)

    store.set_enabled("mid", enabled=False, known=set(_discover(tmp_path)))

    written = json.loads(store.path.read_text())
    assert list(written["plugins"].keys()) == ["alpha", "mid", "zeta"]
    assert written["plugins"]["mid"]["enabled"] is False
    assert written["plugins"]["alpha"]["enabled"] is True


# ── parse_description ──


def test_parse_description_from_plugin_file(tmp_path: Path):
    plugin_py = tmp_path / "plugin.py"
    plugin_py.write_text('__description__ = "test plugin"\n')
    assert parse_description(plugin_py) == "test plugin"


def test_parse_description_falls_back_to_docstring(tmp_path: Path):
    plugin_py = tmp_path / "plugin.py"
    plugin_py.write_text('"""Test docstring.\n\nMore details."""\n')
    assert parse_description(plugin_py) == "Test docstring."


def test_parse_description_empty_when_nothing(tmp_path: Path):
    plugin_py = tmp_path / "plugin.py"
    plugin_py.write_text("x = 1\n")
    assert parse_description(plugin_py) == ""


# ── discover_plugins ──


def test_discover_finds_repo_plugins(tmp_path: Path):
    _make_plugin_dir("myplugin", tmp_path)
    discovered = _discover(tmp_path)
    assert "myplugin" in discovered


def test_discover_duplicate_raises(tmp_path: Path):
    _make_plugin_dir("dup", tmp_path)
    _make_external_plugin("dup", tmp_path)
    with pytest.raises(DuplicatePlugin, match="dup"):
        _discover(tmp_path)


# ── error hierarchy ──


def test_schema_invalid_is_config_error():
    assert issubclass(SchemaInvalid, PluginsConfigError)


def test_dangling_is_config_error():
    assert issubclass(DanglingPlugin, PluginsConfigError)


def test_duplicate_is_config_error():
    assert issubclass(DuplicatePlugin, PluginsConfigError)


# ── per-machine local enable/disable ──


def test_set_local_enabled_writes_local_file(tmp_path: Path, store: PluginEnableStore):
    """Toggling writes the per-machine local file; the flag flips."""

    _make_plugin_dir("compact", tmp_path)
    store.write({"plugins": {"compact": {"enabled": True}}})

    cfg = store.set_enabled("compact", enabled=False, known=set(_discover(tmp_path)))
    assert cfg.plugins["compact"].enabled is False

    on_disk = json.loads(store.path.read_text())
    assert on_disk["plugins"]["compact"]["enabled"] is False


def test_set_local_enabled_scopes_to_local_plugins(tmp_path: Path, store: PluginEnableStore):
    """The written local file references only plugins present on THIS machine,
    so a later load() never raises DanglingPlugin."""

    _make_plugin_dir("compact", tmp_path)
    # A stale local file references a plugin that is NOT on this machine.
    store.write({"plugins": {"compact": {"enabled": True}, "ghost": {"enabled": True}}})

    store.set_enabled("compact", enabled=False, known=set(_discover(tmp_path)))
    cfg = store.load({"compact"})  # would raise DanglingPlugin if 'ghost' survived
    assert "ghost" not in cfg.plugins
    assert cfg.plugins["compact"].enabled is False


def test_set_local_enabled_rejects_unknown(tmp_path: Path, store: PluginEnableStore):
    """A name not present on this machine is refused (no file written)."""

    with pytest.raises(DanglingPlugin):
        store.set_enabled("nonexistent", enabled=True, known=set(_discover(tmp_path)))


# ── corrupt-file philosophy (audit #9) ──


def test_load_raises_on_malformed_json(tmp_path: Path, store: PluginEnableStore):
    """A corrupt local file fails fast instead of silently degrading to the
    all-enabled default — the same rule install_registry applies to
    installed.json (audit #9)."""

    _make_plugin_dir("compact", tmp_path)
    store.path.write_text("{ not json")
    with pytest.raises(json.JSONDecodeError):
        store.load({"compact"})


def test_load_raises_on_non_object_json(tmp_path: Path, store: PluginEnableStore):

    _make_plugin_dir("compact", tmp_path)
    store.path.write_text("[1, 2]")
    with pytest.raises(PluginsConfigError):
        store.load({"compact"})


def test_load_empty_file_is_default_all_enabled(tmp_path: Path, store: PluginEnableStore):

    _make_plugin_dir("compact", tmp_path)
    store.path.write_text("   \n")
    cfg = store.load({"compact"})
    assert cfg.plugins["compact"].enabled is True
