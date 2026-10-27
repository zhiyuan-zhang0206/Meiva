"""The recovery CLI upgrades a retired-only plugin image without loading the plugin runtime.

Split out of `base/packages/tests/test_update_disk_images.py`: it drives `cli.commands.extensions.plugins` against the fleet plugin's real config model."""

import json
from pathlib import Path

import pytest

from base.packages.plugins.config_registration import disk_image_path
from base.paths import meiva_home
from base.paths.tests.homes import point_home


@pytest.fixture(autouse=True)
def _isolate_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The command resolves its home from the process environment, so every test works in a
    temporary home; its external plugins live under `<home>/plugins`."""
    return point_home(monkeypatch, tmp_path)


def _make_plugin_dir(name: str, home: Path) -> Path:
    plugin_dir = home / "plugins" / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.py").write_text(f'__description__ = "{name} plugin"\n')
    return plugin_dir


def test_plugins_update_reconciles_retired_only_image(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The recovery CLI upgrades the old image without loading the plugin runtime."""
    from pydantic import BaseModel

    from cli.commands.extensions.plugins import cmd_plugins_update

    class SampleConfig(BaseModel):
        execution_mode: str = "image"

    plugin_dir = _make_plugin_dir("retired_demo", tmp_path)
    (plugin_dir / "plugin.py").write_text('raise RuntimeError("runtime must not load")\n')
    (plugin_dir / "default_config.py").write_text(
        "from pydantic import BaseModel\n"
        "from base.packages.plugins.extensions import PluginContributions\n\n"
        "class SampleConfig(BaseModel):\n"
        "    execution_mode: str = 'image'\n\n"
        "def contribute() -> PluginContributions:\n"
        "    return PluginContributions(config=SampleConfig)\n"
    )
    image = disk_image_path("retired_demo", home=meiva_home())
    image.parent.mkdir(parents=True)
    image.write_text('{"agent_standing_directives": []}\n')

    assert cmd_plugins_update(home=tmp_path) == 0
    assert json.loads(image.read_text()) == SampleConfig().model_dump()
    saved = image.read_bytes()
    capsys.readouterr()
    assert cmd_plugins_update(home=tmp_path) == 0
    assert "retired_demo: no schema diff" in capsys.readouterr().out
    assert image.read_bytes() == saved
