# meiva:note
# The operation identity a window mints (task #6024): one id per window, the gate verdict the
# target mode implies, and the shell prologue every effect command carries. Pure values, so the
# assertions are about the strings a host will read, not about a window.
# meiva:link cli/docs/fleet-update.meiva.okf.md
from __future__ import annotations

import shlex

from cli.fleet_update.plan.operation import (
    GATE_ENV,
    ID_ENV,
    INITIATOR_ENV,
    OperationIdentity,
    gate_for,
    shell_exports,
)


def test_a_certified_target_satisfies_the_gate_and_track_branch_overrides_it() -> None:
    assert gate_for("channel") == ("satisfied", "channel")
    assert gate_for("release") == ("satisfied", "release")
    assert gate_for("commit") == ("satisfied", "commit")
    assert gate_for("track-branch", branch="preview") == ("overridden", "track-branch:preview")


def test_the_prologue_names_every_identity_variable_as_one_export() -> None:
    op = OperationIdentity(
        operation_id="op-1",
        initiator="user:zy@laptop",
        gate="overridden",
        gate_reason="track-branch:preview",
    )
    line = shell_exports(op)
    assert line.startswith("export ")
    # meiva:note
    # One `export` for all four: a host reads them from one environment, and a per-variable
    # export would let a later edit add a fifth name without noticing the shell sees two forms.
    assert line.count("export ") == 1
    for name in (ID_ENV, INITIATOR_ENV, GATE_ENV):
        assert f"{name}=" in line
    assert "op-1" in line and "user:zy@laptop" in line


def test_an_initiator_with_spaces_survives_the_shell() -> None:
    op = OperationIdentity(
        operation_id="op-2",
        initiator="user:zy ong@lap top",
        gate="satisfied",
        gate_reason="channel",
    )
    line = shell_exports(op)
    # meiva:note
    # The value reaches the host as one word: unquoted, the space would split it and the host
    # would record a truncated initiator that no reader could match to a person. `shlex.split`
    # keeps `NAME=value` together when the value is quoted, which is what this pins.
    words = shlex.split(line.removeprefix("export ").rstrip(";"))
    assert words[1] == f"{INITIATOR_ENV}=user:zy ong@lap top"
