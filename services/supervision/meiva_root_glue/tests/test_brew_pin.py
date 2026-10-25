import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from services.supervision.meiva_root_glue.diagnostic_probes import brew_pins


def test_missing_approved_pin_is_reported() -> None:
    query = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, stdout="postgresql@17\n"),
            subprocess.CompletedProcess([], 0, stdout="postgresql@17\nredis\n"),
        ]
    )
    assert brew_pins(run_cmd=query).verdict.value == "down"
    assert query.call_args_list[0].args[0] == ["brew", "list", "--pinned"]
    assert all(call.kwargs["timeout"] == 5 for call in query.call_args_list)


def test_command_failure_is_not_an_empty_pin_set() -> None:
    query = Mock(side_effect=subprocess.CalledProcessError(1, "brew"))
    with pytest.raises(subprocess.CalledProcessError):
        brew_pins(run_cmd=query)


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
