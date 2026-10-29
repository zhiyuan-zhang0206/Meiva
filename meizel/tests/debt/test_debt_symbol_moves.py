# meiva:note
# A declared symbol move, end to end through `debt diff`: the base's replay reads the moved name at
# its new home, says which import it retargeted, and buys nothing else — a real new dependency the
# change adds is still content growth.

from __future__ import annotations

from pathlib import Path

import pytest

from meizel.tests.synthetic import RepoFactory, commit, run, write

MOVED = {
    "meizel.toml": '[roles]\napplication = ["app"]\ntooling = ["meizel"]\n',
    "app/__init__.py": "",
    "app/lib.py": "def thing() -> int:\n    return 1\n",
    "app/use.py": "from app.lib import thing\n\nVALUE = thing()\n",
    "meizel/rule.py": "RULE = 1\n",
}


PLAIN = {
    "lib/__init__.py": "",
    "lib/_hidden.py": "",
    "app/__init__.py": "",
    "app/x.py": "import lib._hidden\n",
    "rules/engine.py": "RULE = 1\n",
}


def _plain_repo(make_repo: RepoFactory) -> Path:
    root = make_repo(PLAIN)
    commit(root)
    return root


def _record(root: Path, text: str) -> Path:
    path = root.parent / "record.txt"
    path.write_text(text, encoding="utf-8")
    return path


# meiva:note
# The move the tests declare: the function leaves lib.py, box.py defines it, and use.py follows it.
def _move(root: Path) -> None:
    write(
        root,
        {
            "app/lib.py": "",
            "app/box.py": "def thing() -> int:\n    return 1\n",
            "app/use.py": "from app.box import thing\n\nVALUE = thing()\n",
            "meizel/rule.py": "RULE = 2\n",
        },
    )


# meiva:note
# A declaration buys one thing: the base's replay finds the same site at the symbol's new home. It
# does not buy silence: a real new dependency the change adds is still content growth.


def test_a_declared_move_does_not_swallow_a_real_new_dependency(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(MOVED)
    commit(root)
    write(
        root,
        {
            "app/lib.py": "",
            "app/box.py": "def thing() -> int:\n    return 1\n",
            "app/use.py": "from app.box import thing\n\nVALUE = thing()\nimport os\n\nOTHER = os.environ['A']\n",
            "app/meizel.toml": '[debt.ambient-read]\n"use.py#environ os.environ" = 1\n',
            "meizel/rule.py": "RULE = 2\n",
        },
    )
    record = root.parent / "record.txt"
    record.write_text(
        "Symbol-Move: app/lib.py::thing -> app/box.py — the function left the library\n"
    )
    capsys.readouterr()
    assert (
        run(
            root,
            "debt",
            "diff",
            "--against",
            "HEAD",
            "--on-growth",
            "fail",
            "--symbol-moves-file",
            str(record),
        )
        == 1
    )
    out = capsys.readouterr()
    assert "use.py#environ os.environ" in out.out + out.err


# meiva:note
# The positive side: the same move with nothing else added leaves no content growth, and the gate
# says which import it retargeted — the line that lands in the job summary.
def test_a_declared_move_is_measured_at_the_new_home_and_says_so(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(MOVED)
    commit(root)
    write(
        root,
        {
            "app/lib.py": "",
            "app/box.py": "def thing() -> int:\n    return 1\n",
            "app/use.py": "from app.box import thing\n\nVALUE = thing()\n",
            "meizel/rule.py": "RULE = 2\n",
        },
    )
    record = root.parent / "record.txt"
    record.write_text(
        "Symbol-Move: app/lib.py::thing -> app/box.py — the function left the library\n"
    )
    capsys.readouterr()
    assert (
        run(
            root,
            "debt",
            "diff",
            "--against",
            "HEAD",
            "--on-growth",
            "fail",
            "--symbol-moves-file",
            str(record),
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "no content growth" in out
    assert "base import rewritten: app/use.py:1 app.lib.thing -> app.box.thing" in out


# meiva:note
# The declaration is an assertion, not a licence: a move the change did not make is reported and
# changes nothing, so a typo cannot quietly buy silence.
def test_a_declaration_the_change_does_not_make_is_reported_and_ignored(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _plain_repo(make_repo)
    record = _record(root, "Symbol-Move: lib/_hidden.py::Nope -> app/x.py — never happened\n")
    capsys.readouterr()
    assert (
        run(
            root,
            "debt",
            "diff",
            "--against",
            "HEAD",
            "--on-growth",
            "fail",
            "--symbol-moves-file",
            str(record),
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "does not describe this change" in out
