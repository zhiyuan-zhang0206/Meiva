# meiva:note
# Ordinary Python shutdown closes the process-lifetime sampling owner of an installed surface.
import sys
from pathlib import Path

from base.host.proc import run_bounded

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
