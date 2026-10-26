# meiva:note
# Ordinary Python shutdown closes the process-lifetime sampling owner of an installed surface.
import json
import sys
from pathlib import Path

import pytest

from base.clock.tests.seeded_rng import seeded_rng
from base.host.proc import run_bounded
from base.paths.tests.homes import unit_home as unit_home
from meiva.boot import process_sampling_owner

_CHILD = Path(__file__).with_name("child_sampling_atexit.py")
_UNINSTALLED_CHILD = Path(__file__).with_name("child_sampling_atexit_uninstalled.py")


def test_the_installed_process_owner_is_closed_at_interpreter_exit() -> None:
    result = run_bounded(
        [sys.executable, str(_CHILD)],
        capture_output=True,
        text=True,
        timeout=8,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "closed"


def test_uninstalled_surface_leaves_no_atexit_callback() -> None:
    result = run_bounded(
        [sys.executable, str(_UNINSTALLED_CHILD)],
        capture_output=True,
        text=True,
        timeout=8,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "open"


def test_installation_atexit_reports_a_real_read_failure(tmp_path: Path) -> None:
    result = run_bounded(
        [sys.executable, str(_CHILD), "failure", str(tmp_path / ".env")],
        capture_output=True,
        text=True,
        timeout=8,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "closed"
    assert "Exception ignored in atexit callback" in result.stderr
    assert "UnicodeDecodeError" in result.stderr


@pytest.mark.parametrize("mode", ["reload", "reload-explicit", "failed-reload"])
def test_full_reload_preserves_the_process_roots_exit_cleanup(mode: str, unit_home: Path) -> None:
    result = run_bounded(
        [sys.executable, str(_CHILD), mode],
        capture_output=True,
        text=True,
        timeout=8,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "closed"
    stored = (unit_home / "plugins_config.json").read_text()
    if mode == "failed-reload":
        assert stored == "{"
    else:
        plugins = json.loads(stored)["plugins"]
        assert plugins and all(not entry["enabled"] for entry in plugins.values())


def test_full_reload_does_not_register_a_service_owners_cleanup(unit_home: Path) -> None:
    result = run_bounded(
        [sys.executable, str(_CHILD), "service-reload"],
        capture_output=True,
        text=True,
        timeout=8,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "open"
    plugins = json.loads((unit_home / "plugins_config.json").read_text())["plugins"]
    assert plugins and all(not entry["enabled"] for entry in plugins.values())


def test_the_boot_wrapper_keeps_the_generator_its_root_gave_it(unit_home: Path) -> None:
    rng = seeded_rng(4)

    owner = process_sampling_owner(rng)
    try:
        assert owner.rng is rng
    finally:
        owner.close()
