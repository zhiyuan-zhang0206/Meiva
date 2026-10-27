# meiva:note
# meiva.__getattr__ env-gated lazy plugin-namespace load + meiva.ensure_plugins_loaded.
#
# A process an agent launched (MEIVA_AGENT_ID forwarded, no bootstrap to hook — a
# bare `python x.py` in a persistent shell session) self-loads plugin namespaces
# on the first unknown `meiva.X`; gateway / cli / the agent process itself keep the
# fail-fast AttributeError.
#
# These lock the gating matrix + the once-per-process load (the installation slot) so a future edit can't silently
# (a) start loading plugins in the gateway / cli, (b) re-run load_extensions in
# the agent process (which would uninstall and reinstall the whole SDK surface under it), or (c) turn a dunder probe
# into a plugin load.
#
# Each scenario runs in a fresh interpreter (`child_lazy_load.py`) against a real plugin
# directory in the test home. The plugin counts its own executions in a file, so a load that
# happened or did not happen is read from the plugin, not from a replaced loader.
# meiva:link meiva/docs/boot-contracts.meiva.okf.md

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from base.packages.plugins.enable_config import discover_plugins, local_config_path, write_local

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROBE = Path(__file__).with_name("child_lazy_load.py")
_LAUNCH_MARKERS = frozenset(
    {
        "MEIVA_AGENT_ID",
        "MEIVA_PROCESS_PROFILE",
        "MEIVA_EXEC_REQUEST_FILE",
        "MEIVA_EXEC_RESULT_FILE",
        "MEIVA_EXEC_TIMEOUT_S",
        "MEIVA_TURN_ID",
        "MEIVA_SESSION_ID",
        "MEIVA_LOG_DIR",
        "MEIVA_AGENT_LABEL",
        "MEIVA_AGENT_DIR",
        "MEIVA_HARNESS_HANDLE",
    }
)

_PLUGIN = """\
from pathlib import Path
from types import SimpleNamespace

from base.packages.plugins.extensions import PluginContributions, SdkMember, SdkNamespace

with Path(r"{counter}").open("a", encoding="utf-8") as counter:
    counter.write("executed\\n")


def contribute() -> PluginContributions:
    return PluginContributions(
        sdk_namespaces=(
            SdkNamespace("lazytasks", SimpleNamespace(ping=lambda: "pong", __doc__="t")),
        ),
        sdk_members=(
            SdkMember("self", "lazylog", lambda: "pong"),
            SdkMember("ui", "lazynotify", lambda: "pong"),
        ),
    )
"""
_MANIFEST = {
    "apiVersion": 2,
    "name": "lazytasks",
    "version": "1.0.0",
    "engines": {"meiva": ">=0.1.0"},
    "contributions": {"sdkNamespaces": ["lazytasks"]},
}


@pytest.fixture
def counter(unit_home: Path) -> Path:
    """A real plugin in the test home, enabled beside the model-provider plugins; the file counts its executions."""
    directory = unit_home / "plugins" / "lazytasks"
    directory.mkdir(parents=True)
    count_file = unit_home / "lazytasks.executions"
    (directory / "plugin.py").write_text(_PLUGIN.format(counter=count_file), encoding="utf-8")
    (directory / "meiva-plugin.json").write_text(json.dumps(_MANIFEST), encoding="utf-8")
    write_local(
        {
            "plugins": {
                name: {"enabled": name == "lazytasks" or name.startswith("lm_")}
                for name in discover_plugins(home=unit_home)
            }
        },
        home=unit_home,
    )
    return count_file


# meiva:note
# The probe runs a fixed argv from this file; `sys.executable` is the test's own interpreter, so
# the subprocess call carries no shell and no caller input.
def run(scenario: str, counter: Path, unit_home: Path, *, launched: bool) -> dict[str, Any]:
    env = {key: value for key, value in os.environ.items() if key not in _LAUNCH_MARKERS}
    env.update({"MEIVA_HOME": str(unit_home), "MEIVA_CONFIG_FETCH": "skip"})
    if launched:
        env["MEIVA_AGENT_ID"] = "42"
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-B", "-X", "utf8", str(_PROBE), scenario, str(counter)],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_a_launched_child_loads_its_plugins_at_import(counter: Path, unit_home: Path) -> None:
    report = run("loads_at_import", counter, unit_home, launched=True)
    assert report == {"installed_after_import": True, "pong": "pong", "executions": 1}


def test_lazy_load_fires_on_the_first_miss_when_the_entry_did_not_load(
    counter: Path, unit_home: Path
) -> None:
    report = run("loads_on_first_miss", counter, unit_home, launched=True)
    assert report == {
        "installed_after_import": False,
        "pong": "pong",
        "installed": True,
        "executions": 1,
    }


def test_lazy_load_latches_once(counter: Path, unit_home: Path) -> None:
    report = run("latched_after_first_load", counter, unit_home, launched=True)
    assert report["executions"] == 1
    assert "has no attribute 'still_unknown'" in report["second_miss"]


def test_no_lazy_load_without_agent_id(counter: Path, unit_home: Path) -> None:
    report = run("unknown_name", counter, unit_home, launched=False)
    assert report["error"] == "module 'meiva' has no attribute 'nope_xyz'"
    assert report["installed"] is False
    assert report["executions"] == 0


def test_no_lazy_load_in_agent_process(counter: Path, unit_home: Path) -> None:
    report = run("agent_process", counter, unit_home, launched=False)
    assert "has no attribute 'nope_xyz'" in report["error"]
    assert report["executions"] == 0


def test_underscore_names_never_lazy_load(counter: Path, unit_home: Path) -> None:
    report = run("private_name", counter, unit_home, launched=True)
    assert report["error"] is not None
    assert report["installed"] is False
    assert report["executions"] == 0


def test_db_url_forward_wins_over_lazy_load(counter: Path, unit_home: Path) -> None:
    report = run("forwarded_setting", counter, unit_home, launched=True)
    assert report == {"db_url_is_str": True, "executions": 0}


def test_ensure_plugins_loaded_idempotent(counter: Path, unit_home: Path) -> None:
    report = run("ensure_twice", counter, unit_home, launched=False)
    assert report == {"installed": True, "executions": 1}


def test_ensure_plugins_loaded_preserves_a_boot_failure_without_retry(
    counter: Path, unit_home: Path
) -> None:
    local_config_path(home=unit_home).write_text("{not json", encoding="utf-8")
    report = run("unreadable_plugin_config", counter, unit_home, launched=False)
    assert report["raised"] == 2
    assert report["same_error"] is True
    assert report["installed"] is False
    assert report["attempted"] is True
    assert report["executions"] == 0


def test_failed_lazy_lookup_repeats_the_original_boot_error(counter: Path, unit_home: Path) -> None:
    local_config_path(home=unit_home).write_text("{not json", encoding="utf-8")
    report = run("unreadable_config_on_lookup", counter, unit_home, launched=True)
    assert report["raised"] == 2
    assert report["same_error"] is True
    assert report["executions"] == 0


def test_faces_failure_is_not_marked_successful_and_repeats_original_error(
    counter: Path, unit_home: Path
) -> None:
    report = run("faces_failure", counter, unit_home, launched=False)
    assert report["faces"]["raised"] == 3
    assert report["faces"]["same_error"] is True
    assert report["still_the_surface"] is True
    assert report["executions"] == 1


def test_member_lazy_load_on_meiva_self(counter: Path, unit_home: Path) -> None:
    report = run("member_on_framework_namespace", counter, unit_home, launched=True)
    assert report == {"pong": "pong", "executions": 1}


def test_member_fail_fast_outside_child(counter: Path, unit_home: Path) -> None:
    report = run("missing_member", counter, unit_home, launched=False)
    assert report["self"] is not None and report["ui"] is not None
    assert report["executions"] == 0
