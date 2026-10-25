import subprocess
from pathlib import Path

import pytest

from services.supervision.meiva_root_glue.diagnostic_probes import brew_pins


def test_missing_approved_pin_is_reported(tmp_path: Path) -> None:
    executable = tmp_path / "brew"
    executable.write_text(
        "#!/bin/sh\nif [ \"$2\" = \"--pinned\" ]; then\n  printf 'postgresql@17\\n'\nelse\n  printf 'postgresql@17\\nredis\\n'\nfi\n"
    )
    executable.chmod(0o700)
    assert brew_pins(brew_binary=str(executable)).verdict.value == "down"


def test_command_failure_is_not_an_empty_pin_set(tmp_path: Path) -> None:
    executable = tmp_path / "brew"
    executable.write_text("#!/bin/sh\nexit 1\n")
    executable.chmod(0o700)
    with pytest.raises(subprocess.CalledProcessError):
        brew_pins(brew_binary=str(executable))


def test_pin_observation_uses_real_bounded_process_contract(tmp_path: Path) -> None:
    executable = tmp_path / "brew"
    executable.write_text("#!/bin/sh\nprintf 'postgresql@17\\n'\n")
    executable.chmod(0o700)
    assert brew_pins(brew_binary=str(executable)).alive


def test_nonzero_brew_query_is_not_readiness(tmp_path: Path) -> None:
    executable = tmp_path / "brew"
    executable.write_text("#!/bin/sh\nexit 7\n")
    executable.chmod(0o700)
    with pytest.raises(subprocess.CalledProcessError):
        brew_pins(brew_binary=str(executable))
