# meiva:note
# `meiva cluster re-admit-inputs` against a real root-control socket: the line an operator reads
# is the acceptance for the repair command — this home's machine, every path whose digest moved
# with both values, and the caller the root recorded (task #6403).
#
# It lives beside `scripted_root`, the scripted tree it drives: the command's own package may not
# reach into another command's test tree (that edge would close the layering cycle), and what is
# under test is the shell — a real root-control socket — rather than a string.

from __future__ import annotations

import os
from pathlib import Path

import pytest

from base.host.tests.short_tmp import short_tmp as short_tmp
from cli.commands.cluster.re_admit import cmd_re_admit_inputs
from cli.commands.lifecycle.tests.scripted_root import UnitRow, scripted_root

_OLD = "a" * 64
_NEW = "b" * 64


def test_the_report_names_the_machine_every_moved_path_and_the_caller(
    short_tmp: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with scripted_root(short_tmp, [UnitRow("grafana")]) as root:
        root.resources["admit-inputs"] = {
            "unit": "grafana",
            "changed": [["/h/lgtm/native/config", _OLD, _NEW]],
        }
        code = cmd_re_admit_inputs("grafana", home=short_tmp, environ={}, pid=os.getpid())

    printed = capsys.readouterr().out
    assert code == 0
    assert printed.startswith("re-admitted on ")
    assert ": grafana (1 changed input(s))" in printed
    assert f"  /h/lgtm/native/config: {_OLD} -> {_NEW}" in printed
    assert "  caller: " in printed
    assert " the running generation still uses what it started with" in printed


# meiva:note
# An unchanged seal accepts nothing and says so: the operator who ran it expected a repair, and
# "nothing moved" is the answer to a question that was not asked.
def test_no_moved_path_says_the_seal_already_matches(
    short_tmp: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with scripted_root(short_tmp, [UnitRow("grafana")]) as root:
        root.resources["admit-inputs"] = {"unit": "grafana", "changed": []}
        code = cmd_re_admit_inputs("grafana", home=short_tmp, environ={}, pid=os.getpid())

    printed = capsys.readouterr().out
    assert code == 0
    assert "(0 changed input(s))" in printed
    assert "the seal already matches the contents" in printed


# meiva:note
# A root that answers nothing is a home this command cannot reach: the operator gets the reason,
# not a silent success.
def test_a_home_with_no_root_answers_with_the_reason(
    short_tmp: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cmd_re_admit_inputs("grafana", home=short_tmp, environ={}, pid=os.getpid())

    captured = capsys.readouterr()
    assert code == 1
    assert "no root answers this home" in captured.err
