"""`update_all_disk_images`: the plugin disk-image schema merge behind `meiva plugins update`."""

import json
from pathlib import Path

from base.packages.plugin_config_admin import PluginUpdateResult, update_all_disk_images


def _make_plugin_dir(name: str, tmp_path: Path) -> Path:
    root = tmp_path / "repo_plugins"
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.py").write_text(f'__description__ = "{name} plugin"\n')
    return plugin_dir


def _update(tmp_path: Path) -> PluginUpdateResult:
    repo = tmp_path / "repo_plugins"
    user = tmp_path / "user_plugins"
    repo.mkdir(exist_ok=True)
    user.mkdir(exist_ok=True)
    return update_all_disk_images(repo_dir=repo, user_dir=user, home=tmp_path)


# ── update_all_disk_images ──


_DECLARE = (
    "from base.packages.plugins.extensions import PluginContributions\n"
    "def contribute():\n    return PluginContributions(config=Config)\n"
)


def _write_default_config_py(plugin_dir: Path, body: str) -> None:
    """A config face: the plugin's model (`body`) plus the `contribute()` that declares `Config`."""
    (plugin_dir / "default_config.py").write_text(body + _DECLARE)


def test_update_no_plugins_returns_empty(tmp_path: Path):
    result = _update(tmp_path)
    assert result.entries == []


def test_update_skips_plugin_without_default_config(tmp_path: Path):
    _make_plugin_dir("nodefault", tmp_path)
    result = _update(tmp_path)
    assert len(result.entries) == 1
    e = result.entries[0]
    assert e.name == "nodefault"
    assert e.status == "skipped"
    assert e.detail and "default_config.py" in e.detail


def test_update_error_when_the_declared_class_is_not_a_basemodel(tmp_path: Path):
    plugin_dir = _make_plugin_dir("nobm", tmp_path)
    _write_default_config_py(plugin_dir, "class Config:\n    x = 1\n")
    result = _update(tmp_path)
    assert result.entries[0].status == "error"
    assert "BaseModel" in (result.entries[0].detail or "")


def test_update_error_when_the_face_has_no_contribute(tmp_path: Path):
    plugin_dir = _make_plugin_dir("nodecl", tmp_path)
    (plugin_dir / "default_config.py").write_text(
        "from pydantic import BaseModel\nclass Config(BaseModel):\n    x: int = 1\n"
    )
    result = _update(tmp_path)
    assert result.entries[0].status == "error"
    assert "contribute" in (result.entries[0].detail or "")


def test_update_takes_the_declared_class_among_several_models(tmp_path: Path):
    """The declaration names the class; a second model in the file is not an ambiguity."""
    plugin_dir = _make_plugin_dir("multibm", tmp_path)
    _write_default_config_py(
        plugin_dir,
        "from pydantic import BaseModel\n"
        "class Other(BaseModel):\n    y: int = 2\n"
        "class Config(BaseModel):\n    x: int = 1\n",
    )
    result = _update(tmp_path)
    assert result.entries[0].status == "updated"
    assert result.entries[0].added == ["x"]


def test_update_writes_default_when_no_disk_image(tmp_path: Path):
    plugin_dir = _make_plugin_dir("freshplugin", tmp_path)
    _write_default_config_py(
        plugin_dir,
        "from pydantic import BaseModel\n"
        "class Config(BaseModel):\n    timeout: int = 30\n    name: str = 'x'\n",
    )
    result = _update(tmp_path)
    assert len(result.entries) == 1
    e = result.entries[0]
    assert e.name == "freshplugin"
    assert e.status == "updated"
    assert sorted(e.added) == ["name", "timeout"]
    assert e.removed == []


def test_update_no_diff_when_already_in_sync(tmp_path: Path):
    plugin_dir = _make_plugin_dir("synced", tmp_path)
    _write_default_config_py(
        plugin_dir,
        "from pydantic import BaseModel\nclass Config(BaseModel):\n    k: int = 1\n",
    )
    _update(tmp_path)
    result = _update(tmp_path)
    assert result.entries[0].status == "no_diff"


def test_update_removes_fields_deleted_from_schema(tmp_path: Path):
    """Schema shrink: a field still on disk but no longer declared is dropped, so
    the rebuilt image matches the schema's key set and agent boot's strict-equality
    bind check self-heals (the prod spawn-terminated case from #1142)."""
    plugin_dir = _make_plugin_dir("shrunk", tmp_path)
    _write_default_config_py(
        plugin_dir,
        "from pydantic import BaseModel\nclass Config(BaseModel):\n    kept: int = 1\n",
    )
    config_path = tmp_path / "configs" / "shrunk" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps({"kept": 7, "obsolete": "old"}))

    result = _update(tmp_path)

    entry = result.entries[0]
    assert entry.status == "updated"
    assert entry.added == []
    assert entry.removed == ["obsolete"]
    assert json.loads(config_path.read_text()) == {"kept": 7}


def test_update_isolates_errors_between_plugins(tmp_path: Path):
    """One plugin erroring does not block other plugins — entries have mixed status."""
    good_dir = _make_plugin_dir("good", tmp_path)
    _write_default_config_py(
        good_dir,
        "from pydantic import BaseModel\nclass Config(BaseModel):\n    a: int = 1\n",
    )
    bad_dir = _make_plugin_dir("bad", tmp_path)
    _write_default_config_py(bad_dir, "raise RuntimeError('plugin author oops')\n")
    result = _update(tmp_path)
    by_name = {e.name: e for e in result.entries}
    assert by_name["good"].status == "updated"
    assert by_name["bad"].status == "error"
    assert "plugin author oops" in (by_name["bad"].detail or "")
