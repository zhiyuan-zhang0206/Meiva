from __future__ import annotations

from pathlib import Path

import pytest

from cli.commands.converge import incomplete
from cli.commands.lifecycle import converge_host
from cli.commands.lifecycle.status import print_converge_section

_STEP = "permissions helper build + sign + load"


# meiva:note
# The marker is what makes an aborted converge visible after `meiva start` has
# brought the services up: the step name and its reason survive in the home.
# The loop that records it on a failed step is tested with the selection loop.
# meiva:link cli/commands/converge/docs/converge-incomplete.meiva.okf.md
def test_the_prod_install_records_the_failing_step(tmp_path: Path) -> None:
    home = tmp_path / "home"

    converge_host.record_incomplete(home, step=_STEP, error="keychain locked", prod_install=True)

    recorded = incomplete.read_incomplete(home)
    assert recorded is not None
    assert recorded.step == _STEP
    assert "keychain locked" in recorded.error


def test_a_dev_cluster_never_writes_the_marker(tmp_path: Path) -> None:
    home = tmp_path / "home"

    converge_host.record_incomplete(home, step=_STEP, error="keychain locked", prod_install=False)

    assert incomplete.read_incomplete(home) is None


def test_status_prints_nothing_when_the_home_converged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    print_converge_section(tmp_path)

    assert capsys.readouterr().out == ""


def test_status_prints_the_incomplete_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    incomplete.record_incomplete(tmp_path, step=_STEP, error="the login keychain is not unlocked")

    print_converge_section(tmp_path)

    out = capsys.readouterr().out
    assert out.startswith("converge: INCOMPLETE ")
    assert _STEP in out and "keychain is not unlocked" in out
