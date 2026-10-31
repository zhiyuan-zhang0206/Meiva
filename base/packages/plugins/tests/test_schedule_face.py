from __future__ import annotations

import json
from pathlib import Path

import pytest

from base.packages.plugins.loading.schedule_face import (
    declared_schedules,
    declared_ui_stats,
    load_enabled_plugin_schedules,
    load_plugin_ui_stats_safe,
)


def _make_plugin(
    tmp_path: Path,
    name: str,
    *,
    contributions: dict[str, object],
    schedules_code: str | None = None,
    stats_code: str | None = None,
    config_code: str | None = None,
) -> Path:
    pdir = tmp_path / "plugins" / name
    pdir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "apiVersion": 2,
        "name": name,
        "version": "1.0.0",
        "description": f"Test plugin {name}",
        "engines": {"meiva": ">=2026.10.1"},
        "contributions": contributions,
    }
    (pdir / "meiva-plugin.json").write_text(json.dumps(manifest))
    (pdir / "plugin.py").write_text("# plugin\n")
    if schedules_code is not None:
        (pdir / "schedules.py").write_text(schedules_code)
    if stats_code is not None:
        (pdir / "ui_stats.py").write_text(stats_code)
    if config_code is not None:
        (pdir / "default_config.py").write_text(config_code)
    return pdir


def test_declared_schedules_valid(tmp_path: Path) -> None:
    code = (
        "from base.packages.plugins.surface.schedules import ScheduleSpec\n\n"
        "def contribute(config):\n"
        "    return (\n"
        "        ScheduleSpec(\n"
        "            name='my_plugin:worker',\n"
        "            machine='gateway',\n"
        "            description='Sample worker',\n"
        "            script='print(\"hello\")\\n',\n"
        "            command='python script.py',\n"
        "            secret_env_vars=('API_KEY',),\n"
        "            default_enabled=True,\n"
        "        ),\n"
        "    )\n"
    )
    pdir = _make_plugin(
        tmp_path,
        "my_plugin",
        contributions={"schedules": {"face": "schedules.py"}},
        schedules_code=code,
    )
    specs = declared_schedules("my_plugin", pdir, home=tmp_path)
    assert len(specs) == 1
    assert specs[0].name == "my_plugin:worker"
    assert specs[0].machine == "gateway"
    assert specs[0].secret_env_vars == ("API_KEY",)


def test_declared_schedules_dash_prefix(tmp_path: Path) -> None:
    code = (
        "from base.packages.plugins.surface.schedules import ScheduleSpec\n\n"
        "def contribute(config):\n"
        "    return (\n"
        "        ScheduleSpec(\n"
        "            name='my_plugin-worker',\n"
        "            machine='gateway',\n"
        "            description='Sample worker',\n"
        "            script='x = 1\\n',\n"
        "            command='python script.py',\n"
        "        ),\n"
        "    )\n"
    )
    pdir = _make_plugin(
        tmp_path,
        "my_plugin",
        contributions={"schedules": {"face": "schedules.py"}},
        schedules_code=code,
    )
    specs = declared_schedules("my_plugin", pdir, home=tmp_path)
    assert len(specs) == 1
    assert specs[0].name == "my_plugin-worker"


def test_declared_schedules_invalid_prefix_rejected(tmp_path: Path) -> None:
    code = (
        "from base.packages.plugins.surface.schedules import ScheduleSpec\n\n"
        "def contribute(config):\n"
        "    return (\n"
        "        ScheduleSpec(\n"
        "            name='other_plugin:worker',\n"
        "            machine='gateway',\n"
        "            description='Sample worker',\n"
        "            script='x = 1\\n',\n"
        "            command='python script.py',\n"
        "        ),\n"
        "    )\n"
    )
    pdir = _make_plugin(
        tmp_path,
        "my_plugin",
        contributions={"schedules": {"face": "schedules.py"}},
        schedules_code=code,
    )
    with pytest.raises(ValueError, match="must start with 'my_plugin:' or 'my_plugin-'"):
        declared_schedules("my_plugin", pdir, home=tmp_path)


def test_declared_schedules_duplicate_name_rejected(tmp_path: Path) -> None:
    code = (
        "from base.packages.plugins.surface.schedules import ScheduleSpec\n\n"
        "def contribute(config):\n"
        "    return (\n"
        "        ScheduleSpec(\n"
        "            name='my_plugin:one',\n"
        "            machine='gateway',\n"
        "            description='First',\n"
        "            script='x = 1\\n',\n"
        "            command='python script.py',\n"
        "        ),\n"
        "        ScheduleSpec(\n"
        "            name='my_plugin:one',\n"
        "            machine='worker',\n"
        "            description='Duplicate',\n"
        "            script='x = 2\\n',\n"
        "            command='python script.py',\n"
        "        ),\n"
        "    )\n"
    )
    pdir = _make_plugin(
        tmp_path,
        "my_plugin",
        contributions={"schedules": {"face": "schedules.py"}},
        schedules_code=code,
    )
    with pytest.raises(ValueError, match="duplicate schedule name 'my_plugin:one'"):
        declared_schedules("my_plugin", pdir, home=tmp_path)


def test_declared_schedules_script_syntax_error(tmp_path: Path) -> None:
    code = (
        "from base.packages.plugins.surface.schedules import ScheduleSpec\n\n"
        "def contribute(config):\n"
        "    return (\n"
        "        ScheduleSpec(\n"
        "            name='my_plugin:broken',\n"
        "            machine='gateway',\n"
        "            description='Broken',\n"
        "            script='def broken(syntax\\n',\n"
        "            command='python script.py',\n"
        "        ),\n"
        "    )\n"
    )
    pdir = _make_plugin(
        tmp_path,
        "my_plugin",
        contributions={"schedules": {"face": "schedules.py"}},
        schedules_code=code,
    )
    with pytest.raises(ValueError, match="script has Python syntax errors"):
        declared_schedules("my_plugin", pdir, home=tmp_path)


def test_declared_ui_stats_valid(tmp_path: Path) -> None:
    code = (
        "from base.packages.plugins.surface.schedules import StatCardSpec\n\n"
        "def contribute(config):\n"
        "    return (\n"
        "        StatCardSpec(id='stat-a', label='Stat A'),\n"
        "        StatCardSpec(id='stat-b', label='Stat B'),\n"
        "    )\n"
    )
    pdir = _make_plugin(
        tmp_path,
        "my_plugin",
        contributions={"ui": {"statsFace": "ui_stats.py"}},
        stats_code=code,
    )
    cards = declared_ui_stats("my_plugin", pdir, home=tmp_path)
    assert len(cards) == 2
    assert cards[0].id == "stat-a"
    assert cards[1].id == "stat-b"


def test_declared_ui_stats_duplicate_rejected(tmp_path: Path) -> None:
    code = (
        "from base.packages.plugins.surface.schedules import StatCardSpec\n\n"
        "def contribute(config):\n"
        "    return (\n"
        "        StatCardSpec(id='stat-dup', label='Stat A'),\n"
        "        StatCardSpec(id='stat-dup', label='Stat B'),\n"
        "    )\n"
    )
    pdir = _make_plugin(
        tmp_path,
        "my_plugin",
        contributions={"ui": {"statsFace": "ui_stats.py"}},
        stats_code=code,
    )
    with pytest.raises(ValueError, match="duplicate stat card id 'stat-dup'"):
        declared_ui_stats("my_plugin", pdir, home=tmp_path)


def test_load_enabled_plugin_schedules(tmp_path: Path) -> None:
    code = (
        "from base.packages.plugins.surface.schedules import ScheduleSpec\n\n"
        "def contribute(config):\n"
        "    return (\n"
        "        ScheduleSpec(\n"
        "            name='plug_a:worker',\n"
        "            machine='gateway',\n"
        "            description='Worker A',\n"
        "            script='print(1)\\n',\n"
        "            command='python script.py',\n"
        "        ),\n"
        "    )\n"
    )
    _make_plugin(
        tmp_path,
        "plug_a",
        contributions={"schedules": {"face": "schedules.py"}},
        schedules_code=code,
    )
    _make_plugin(
        tmp_path,
        "plug_b",
        contributions={"schedules": {"face": "schedules.py"}},
        schedules_code="raise RuntimeError('broken load')",
    )
    (tmp_path / "plugins_config.json").write_text(json.dumps({"plug_a": True, "plug_b": True}))

    specs_by_plugin, failed = load_enabled_plugin_schedules(home=tmp_path)
    assert "plug_a" in specs_by_plugin
    assert len(specs_by_plugin["plug_a"]) == 1
    assert "plug_b" in failed


def test_load_plugin_ui_stats_safe(tmp_path: Path) -> None:
    recorded_failures: list[tuple[str, BaseException]] = []
    pdir = _make_plugin(
        tmp_path,
        "broken_stats",
        contributions={"ui": {"statsFace": "ui_stats.py"}},
        stats_code="raise RuntimeError('cannot import')",
    )
    result = load_plugin_ui_stats_safe(
        "broken_stats",
        pdir,
        home=tmp_path,
        report=lambda name, exc: recorded_failures.append((name, exc)),
    )
    assert result == ()
    assert len(recorded_failures) == 1
    assert recorded_failures[0][0] == "broken_stats"
