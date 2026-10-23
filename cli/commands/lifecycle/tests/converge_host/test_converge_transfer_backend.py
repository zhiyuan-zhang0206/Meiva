# meiva:note
# The cross-machine transfer probe: which hosts it applies to, and what it says when it fails.

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import cli.commands.lifecycle.converge_host as cv
from cli.commands.converge.plans.split_host_checks import (
    drive_unavailable_warning,
    probes_cross_machine_transfer,
)
from cli.commands.converge.spec import ConvergeCtx
from cli.commands.lifecycle.tests.converge_host.context_inputs import capable_mac

# meiva:note
# --- which hosts are probed (pure) --------------------------------------------------


@pytest.mark.parametrize(
    ("roles", "backend", "probed"),
    [
        (frozenset({"agent-runner"}), "drive", True),
        # meiva:note
        # a unit that is not configured yet is not a single box
        (None, "drive", True),
        # meiva:note
        # no peer to transfer to
        (frozenset({"gateway", "agent-runner"}), "drive", False),
        (frozenset({"gateway"}), "drive", False),
        # meiva:note
        # the operator skips the probe
        (frozenset({"agent-runner"}), "none", False),
    ],
)
def test_only_a_split_runner_with_a_backend_is_probed(
    roles: frozenset[str] | None, backend: str, probed: bool
) -> None:
    assert probes_cross_machine_transfer(roles=roles, backend=backend) is probed


def test_the_warning_lists_where_it_looked_and_how_to_skip_the_probe() -> None:
    warning = drive_unavailable_warning(("/Users/a/Google Drive", "/Users/a/gdrive"))

    assert "no writable Google Drive synced folder" in warning
    assert "will not work" in warning
    assert "MEIVA_CROSS_MACHINE_TRANSFER_BACKEND=none" in warning
    assert warning.endswith("Looked in: /Users/a/Google Drive, /Users/a/gdrive")


def test_the_warning_says_so_when_there_was_nowhere_to_look() -> None:
    assert drive_unavailable_warning(()).endswith("Looked in: (no candidate paths)")


# meiva:note
# --- the step on a host with a real home --------------------------------------------


def _mac_runner(ctx: ConvergeCtx, user_home: Path) -> ConvergeCtx:
    user_home.mkdir()
    return replace(ctx, host=capable_mac())


def test_passes_when_writable_drive_found(
    ctx: ConvergeCtx, user_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = _mac_runner(ctx, user_home)
    drive = user_home / "Google Drive"
    drive.mkdir()

    cv.ensure_cross_machine_transfer(runner)

    assert capsys.readouterr().err == ""
    # meiva:note
    # the write probe cleaned up after itself
    assert list(drive.iterdir()) == []


def test_warns_but_continues_when_drive_missing(
    ctx: ConvergeCtx, user_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cv.ensure_cross_machine_transfer(_mac_runner(ctx, user_home))

    err = capsys.readouterr().err
    assert "no writable Google Drive synced folder" in err
    assert "will not work" in err
    assert "(no candidate paths)" in err


def test_skips_on_single_box(
    ctx: ConvergeCtx, user_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    single_box = replace(_mac_runner(ctx, user_home), roles=frozenset({"gateway", "agent-runner"}))

    # meiva:note
    # no drive on this home, and no warning
    cv.ensure_cross_machine_transfer(single_box)

    assert capsys.readouterr().err == ""


def test_skips_when_backend_none(
    ctx: ConvergeCtx, user_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = _mac_runner(ctx, user_home)
    runner.config.set_field("cross_machine_transfer_backend", "none")

    cv.ensure_cross_machine_transfer(runner)

    assert capsys.readouterr().err == ""


def test_step_registered_agent_runner_only() -> None:
    step = next(s for s in cv.CONVERGE_STEPS if s.name == "cross-machine transfer backend")
    assert step.roles == frozenset({"agent-runner"})
    assert step.requires_unit_config is True
