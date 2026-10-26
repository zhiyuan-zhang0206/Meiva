from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from base import paths
from base.clock.tests.seeded_rng import seeded_rng
from base.packages.plugins import load_report
from base.packages.plugins.enable_config import (
    discover_plugin_dirs,
    local_config_path,
    write_local,
)
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from cli.commands.extensions.plugins_inspect import cmd_plugins_verify

_PLUGIN_MODULE_PREFIXES = ("meiva_builtins.plugins.", "plugins.")


# meiva:note
# The home is this test's own (`unit_home`), so the plugins it writes are the only external
# ones. The repository's built-in plugins are discovered too, and each starts disabled here:
# a disabled plugin is never imported, so the verdict is about the plugins a test wrote.
@pytest.fixture(autouse=True)
def _isolate_plugins(unit_home: Path) -> Iterator[None]:
    (unit_home / "plugins").mkdir()
    write_local({"plugins": {name: {"enabled": False} for name in _builtin_plugin_names()}})
    before = {k: v for k, v in sys.modules.items() if k.startswith(_PLUGIN_MODULE_PREFIXES)}
    yield
    for key in [
        k for k in sys.modules if k.startswith(_PLUGIN_MODULE_PREFIXES) and k not in before
    ]:
        del sys.modules[key]
    sys.modules.update(before)


def _builtin_plugin_names() -> list[str]:
    return sorted(
        discover_plugin_dirs(repo_dir=paths.repo_plugins_dir(), user_dir=Path("/nonexistent"))
    )


def _plugin(name: str, plugin_py: str, **siblings: str) -> None:
    directory = paths.plugins_dir() / name
    directory.mkdir()
    (directory / "plugin.py").write_text(plugin_py)
    (directory / "meiva-plugin.json").write_text(
        json.dumps(
            {
                "apiVersion": 2,
                "name": name,
                "version": "1.0.0",
                "engines": {"meiva": ">=0.1.0"},
            }
        )
    )
    for filename, content in siblings.items():
        (directory / filename).write_text(content)


def _configure(*, enabled: tuple[str, ...] = (), disabled: tuple[str, ...] = ()) -> None:
    write_local(
        {
            "plugins": {
                **{name: {"enabled": False} for name in _builtin_plugin_names()},
                **{name: {"enabled": False} for name in disabled},
                **{name: {"enabled": True} for name in enabled},
            }
        }
    )


def _enable(*names: str) -> None:
    _configure(enabled=names)


def test_loadable_plugins_are_green(capsys: pytest.CaptureFixture[str]) -> None:
    _plugin("good", "LOADED = True\n")
    _enable("good")

    assert cmd_plugins_verify(rng=seeded_rng()) == 0

    assert "RESULT enabled=1 failed=0 rc=0" in capsys.readouterr().out


def test_a_plugin_using_a_deleted_framework_api_is_red_and_the_rest_still_count(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _plugin("good", "LOADED = True\n")
    _plugin(
        "old_hooks",
        "from agent.hooks import register_after_exec\nregister_after_exec(lambda: None)\n",
    )
    _enable("good", "old_hooks")

    assert cmd_plugins_verify(rng=seeded_rng()) == 1

    out = capsys.readouterr().out
    assert "RESULT enabled=2 failed=1 rc=1" in out
    red = next(line for line in out.splitlines() if line.startswith("RED "))
    assert red.startswith(
        "RED plugin=old_hooks ImportError: cannot import name 'register_after_exec'"
    )
    assert "old_hooks/plugin.py:1" in red


def test_a_plugin_without_a_manifest_is_red(capsys: pytest.CaptureFixture[str]) -> None:
    _plugin("good", "LOADED = True\n")
    _plugin("bare", "LOADED = True\n")
    (paths.plugins_dir() / "bare" / "meiva-plugin.json").unlink()
    _enable("good", "bare")

    assert cmd_plugins_verify(rng=seeded_rng()) == 1

    out = capsys.readouterr().out
    assert "RESULT enabled=2 failed=1 rc=1" in out
    assert "RED plugin=bare ManifestError" in out


def test_a_broken_runtime_face_is_red_too(capsys: pytest.CaptureFixture[str]) -> None:
    _plugin("half", "LOADED = True\n", **{"agent_runtime.py": "from agent.hooks import gone\n"})
    _enable("half")

    assert cmd_plugins_verify(rng=seeded_rng()) == 1

    assert "RED plugin=half ImportError" in capsys.readouterr().out


def test_an_enabled_plugin_missing_from_disk_is_red(capsys: pytest.CaptureFixture[str]) -> None:
    _enable("vanished")

    assert cmd_plugins_verify(rng=seeded_rng()) == 1

    assert "RED plugin=vanished" in capsys.readouterr().out


def test_a_disabled_broken_plugin_is_never_imported(capsys: pytest.CaptureFixture[str]) -> None:
    _plugin("old_hooks", "raise RuntimeError('boom')\n")
    _configure(disabled=("old_hooks",))

    assert cmd_plugins_verify(rng=seeded_rng()) == 0

    assert "RESULT enabled=0 failed=0 rc=0" in capsys.readouterr().out


def test_nothing_is_emitted_while_verifying_and_the_canonical_reporter_is_unchanged(
    emitted_events: EmittedEvents,
) -> None:
    _plugin("old_hooks", "raise RuntimeError('boom')\n")
    _enable("old_hooks")

    assert cmd_plugins_verify(rng=seeded_rng()) == 1
    assert len(emitted_events) == 0

    load_report.report_plugin_load_failure("elsewhere", RuntimeError("later"))
    assert emitted_events.names() == ["plugin_load_failed"]


def test_a_loader_crash_is_a_tool_error_not_a_red(capsys: pytest.CaptureFixture[str]) -> None:
    local_config_path().write_text("{ this is not json")

    assert cmd_plugins_verify(rng=seeded_rng()) == 2

    out = capsys.readouterr().out
    assert "TOOL-ERROR" in out and "RESULT enabled=0 failed=0 rc=2" in out
