# meiva:note
# Actual private plist writes; the command resource refuses every scheduler command.

import subprocess
import sys
from pathlib import Path


def _run(scenario: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "user & <private>"
    home.mkdir()
    # meiva:note
    # Fixed interpreter and local child; scenarios are literals from this file, never shell input.
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-I", str(Path(__file__).with_name("child_definition.py")), scenario],
        env={
            "HOME": str(home),
            "MEIVA_HOME": str(home / ".meiva"),
            "MEIVA_CONFIG_FETCH": "skip",
            "PATH": "/usr/bin:/bin",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def test_register_macos_writes_plist_but_never_bootstraps(tmp_path: Path) -> None:
    _run("write", tmp_path)


def test_register_macos_prints_the_reload_form_too(tmp_path: Path) -> None:
    result = _run("write", tmp_path)
    plist = tmp_path / "user & <private>" / "Library/LaunchAgents/com.meiva.autostart.plist"
    assert f"launchctl bootstrap gui/1000 {plist}" in result.stdout
    assert f"launchctl bootout gui/1000/{plist.stem}" in result.stdout
    assert "OLD definition" in result.stdout


def test_register_macos_idempotent_no_rewrite(tmp_path: Path) -> None:
    _run("idempotent", tmp_path)


def test_an_empty_selection_is_refused_and_leaves_the_boot_job_in_place(tmp_path: Path) -> None:
    _run("empty", tmp_path)


def test_definition_import_does_not_load_host_or_boot_resources(tmp_path: Path) -> None:
    _run("cold", tmp_path)
