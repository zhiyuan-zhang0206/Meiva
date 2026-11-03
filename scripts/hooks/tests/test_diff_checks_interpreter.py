# meiva:note
# Which Python a receipt's children run on: the caller's value for every command, including the
# ones that name a first-party entry -- those keep their fact edges through the `Interpreter`
# parameter `diff_checks_plan.py` declares. Each test hands in an interpreter no module of this
# package could have read, so a read that crept back in reddens instead of passing quietly.

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal

from scripts.hooks.diff_checks import ChangeSet, Selection, check, plan
from scripts.hooks.diff_checks_plan import schedule_groups
from scripts.hooks.diff_checks_pytest import pytest_argv

# meiva:note
# A path this machine does not have: a command built from it can only come from the value handed
# in, and any command built from `sys.executable` differs from it visibly.
_ELSEWHERE = "/opt/elsewhere/bin/python"


def _change(*files: str) -> ChangeSet:
    return ChangeSet(root=Path("/repo"), base="origin/main", files=files)


def _always(selection: Selection) -> Callable[[ChangeSet], Selection]:
    def choose(_change: ChangeSet) -> Selection:
        return selection

    return choose


def _commands(
    change: ChangeSet, *, mode: Literal["incremental", "full"], python: str
) -> dict[str, tuple[str, ...]]:
    groups = schedule_groups(change, mode=mode, debt_reduced=True, python=python)
    return {run.label: run.argv for group in groups for run in group.runs}


# meiva:note
# A command that starts a Python interpreter, read the way the checker reads one: its program is
# `python`, `python3` or a version of it. The tools beside the interpreter (basedpyright, ruff,
# pnpm, npx) are their own programs and are not counted here.
def _python_commands(argvs: Sequence[tuple[str, ...]]) -> list[tuple[str, ...]]:
    return [argv for argv in argvs if argv[0].rsplit("/", 1)[-1].startswith("python")]


# meiva:note
# Two changes, because they reach different builders: a Python-only change inside a real checkout
# scopes the checker to `--changed`, while a manifest change makes it read the whole tree.
def test_the_checker_runs_on_the_interpreter_the_caller_handed_in(tmp_path: Path) -> None:
    (tmp_path / "base").mkdir()
    (tmp_path / "base" / "thing.py").write_text("")
    scoped = ChangeSet(root=tmp_path, base="origin/main", files=("base/thing.py",))
    whole_tree = ChangeSet(
        root=tmp_path, base="origin/main", files=("base/thing.py", "base/meizel.toml")
    )

    for change in (scoped, whole_tree):
        for mode in ("incremental", "full"):
            commands = _commands(change, mode=mode, python=_ELSEWHERE)

            assert commands["meizel"][0] == _ELSEWHERE, (
                f"a {len(change.files)}-file {mode} plan started the checker on an interpreter "
                "nobody handed it"
            )
            if "debt" in commands:
                assert commands["debt"][0] == _ELSEWHERE, (
                    f"the {mode} manifest comparison runs where every other check runs"
                )


# meiva:note
# The commands that name a first-party entry take the caller's interpreter too. The edge those
# commands are the only evidence for (`hooks -> audit`, `hooks -> content_lint`) now comes from the
# `Interpreter` parameter they declare, which `meizel check` enforces; this test pins the value, so
# a read that crept back in reddens here instead of passing quietly.
def test_a_command_naming_a_first_party_entry_runs_on_the_interpreter_the_caller_handed_in() -> (
    None
):
    commands = _commands(
        _change("base/thing.py", "base/meizel.toml", "docs/readme.md"),
        mode="incremental",
        python=_ELSEWHERE,
    )

    for label in ("docs-refs", "docs-okf", "refusal-loss"):
        assert commands[label][0] == _ELSEWHERE, (
            f"{label} names a first-party entry: its command runs on the interpreter the caller "
            "handed in, and its edge comes from the `Interpreter` parameter"
        )


def test_the_printed_plan_carries_the_interpreter_the_caller_handed_in() -> None:
    lines = plan(_change("docs/readme.md"), skip_tests=True, python=_ELSEWHERE)

    assert any(_ELSEWHERE in line for line in lines), (
        "the dry run a reviewer reads is the same plan the runner executes; a plan printed under "
        "another interpreter is not the run it claims to describe"
    )


# meiva:note
# The tools a check runs are installed beside the interpreter that runs it, so the directory they
# are looked for comes from the handed-in value: a receipt written for one interpreter must not
# claim a tool that belongs to another.
def test_a_check_tool_is_looked_for_beside_the_interpreter_the_caller_handed_in(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    basedpyright = bin_dir / "basedpyright"
    basedpyright.write_text("#!/bin/sh\n")
    basedpyright.chmod(0o755)

    def pyright_argv(python: str) -> tuple[str, ...]:
        groups = schedule_groups(
            _change("base/thing.py"), mode="incremental", debt_reduced=False, python=python
        )
        return next(run.argv for group in groups for run in group.runs if run.label == "pyright")

    assert pyright_argv(str(bin_dir / "python"))[0] == str(basedpyright), (
        "a tool beside the handed-in interpreter is the one this receipt runs"
    )
    assert pyright_argv(str(tmp_path / "elsewhere" / "python"))[0] == "basedpyright", (
        "with no such tool beside it, the bare name is asked for on PATH -- never a tool found "
        "beside some other interpreter"
    )


def test_the_receipt_starts_pytest_on_the_interpreter_it_was_given() -> None:
    started: list[tuple[str, ...]] = []

    def execute(argv: Sequence[str], root: Path) -> tuple[int, str, float]:
        started.append(tuple(argv))
        return 0, "0 errors, 0 warnings", 1.0

    check(
        _change("base/thing.py"),
        skip_tests=False,
        execute=execute,
        choose=_always(Selection("SELECTED", ("tests/test_a.py",), ())),
        python=_ELSEWHERE,
    )

    python_started = _python_commands(started)
    assert any(argv[1:3] == ("-m", "pytest") for argv in python_started), (
        "the receipt has to run pytest for this test to say anything about it"
    )
    assert {argv[0] for argv in python_started} == {_ELSEWHERE}, (
        f"the receipt started a child on an interpreter nobody handed it: {python_started}"
    )


def test_pytest_argv_names_the_interpreter_it_was_given(tmp_path: Path) -> None:
    argv = pytest_argv(tmp_path / "report.xml", ("tests/test_a.py",), python=_ELSEWHERE)

    assert argv[:3] == (_ELSEWHERE, "-m", "pytest")
