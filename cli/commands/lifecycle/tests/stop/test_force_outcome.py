# meiva:note
# How a force stop ends when a step reports failure through its return code, as values.
#
# The shell that runs a real force stop against a Redis that keeps answering is in
# `test_force_stop_data_plane.py`.

from __future__ import annotations

import pytest

from cli.commands.lifecycle.stop.force_outcome import data_plane_failure, force_incomplete_report


def test_a_data_plane_that_stopped_is_no_failure() -> None:
    assert data_plane_failure(0) is None


@pytest.mark.parametrize("returncode", [1, 2, 255])
def test_a_data_plane_that_did_not_stop_is_a_failure_that_names_its_code(returncode: int) -> None:
    failure = data_plane_failure(returncode)

    assert failure is not None
    assert f"exit code {returncode}" in failure


def test_the_report_says_what_failed_without_claiming_nothing_was_killed() -> None:
    report = force_incomplete_report("the data plane did not stop (exit code 1)")

    assert report.startswith("Force stop incomplete: the data plane did not stop (exit code 1).")
    assert "not force-killed" not in report
    assert "Retry the command" in report
