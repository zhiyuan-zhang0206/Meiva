# meiva:note
# A survivor's role is read where the report is built: the shell and job words the pty closure
# sends, and nothing else. Reading the live facts of a real process is asserted in
# `lifecycle/tests/stop/test_maintenance_stop_report.py`.

from __future__ import annotations

import os

import pytest

from base.native_process.ownership import OwnedProcess
from cli.commands.data_plane.maintenance.survivors import SurvivorRole
from cli.commands.data_plane.maintenance_stop_report import capture_survivor


def test_the_roles_are_the_two_words_the_closure_sends() -> None:
    assert {role.value for role in SurvivorRole} == {"shell", "job"}


@pytest.mark.parametrize(
    ("raw", "role"), [("shell", SurvivorRole.SHELL), ("job", SurvivorRole.JOB)]
)
def test_a_survivor_carries_the_role_its_wire_word_names(raw: str, role: SurvivorRole) -> None:
    identity = OwnedProcess(os.getpid(), 1.0, None)

    assert capture_survivor(identity, service="shell-session", role=raw).role is role


@pytest.mark.parametrize("raw", ["helper", "", "SHELL"])
def test_a_role_outside_the_closures_vocabulary_is_refused_not_taken_for_a_job(raw: str) -> None:
    identity = OwnedProcess(os.getpid(), 1.0, None)

    with pytest.raises(ValueError, match="is not a valid SurvivorRole"):
        capture_survivor(identity, service="shell-session", role=raw)
