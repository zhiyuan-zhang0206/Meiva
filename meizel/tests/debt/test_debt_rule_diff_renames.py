# meiva:note
# `debt diff` across a rename: the content moves, the measurement does not. Each interval here is a
# rule change, so the two measures are printed, and each test moves files the change already has to
# touch. A moved file is measured at the path the change gives it, from the bytes the reference had,
# so a rename is neither a rule change nor repayment of debt.

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from meizel.tests.synthetic import RepoFactory, commit, git, run, write


# meiva:note
# `debt diff` without extra pathspecs: it has to detect the rule change by itself.
def plain_diff(root: Path, on_growth: str = "fail") -> int:
    return run(root, "debt", "diff", "--against", "HEAD", "--on-growth", on_growth)


# meiva:note
# A record of one ambient read the file's own manifest declares, and rule code the change edits.
DECLARED = {
    "meizel.toml": '[roles]\napplication = ["app"]\ntooling = ["meizel"]\n',
    "app/__init__.py": "",
    "app/daemon.py": "import os\n\nvalue = os.environ['A']\n",
    "app/meizel.toml": '[debt.ambient-read]\n"daemon.py#environ os.environ" = 1\n',
    "meizel/rule.py": "RULE = 1\n",
}


# meiva:note
# The interval is a rule change; the rename only changes where the same debt lives.
def test_a_moved_file_keeps_its_debt_out_of_the_content_column(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(DECLARED)
    commit(root)
    write(root, {"meizel/rule.py": "RULE = 2\n"})
    git(root, "mv", "app/daemon.py", "app/worker.py")
    assert run(root, "debt", "carry", "--base", "HEAD") == 0
    git(root, "add", "-A")
    assert "worker.py#environ os.environ" in (root / "app" / "meizel.toml").read_text()
    capsys.readouterr()
    assert plain_diff(root) == 0
    out = capsys.readouterr().out
    assert "no content growth" in out
    assert "meizel: rules +0 / content +0" in out


DIRECTORY = {
    "meizel.toml": '[roles]\napplication = ["app"]\ntooling = ["meizel"]\n',
    "app/__init__.py": "",
    "app/inner/__init__.py": "",
    "app/inner/x.py": "import os\n\nVALUE = os.environ['A']\n",
    "app/inner/meizel.toml": '[debt.ambient-read]\n"x.py#environ os.environ" = 1\n',
    "meizel/rule.py": "RULE = 1\n",
}


def test_a_renamed_directory_takes_its_recorded_debt_along(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(DIRECTORY)
    commit(root)
    git(root, "mv", "app/inner", "app/moved")
    write(root, {"meizel/rule.py": "RULE = 2\n"})
    assert run(root, "debt", "carry", "--base", "HEAD") == 0
    git(root, "add", "-A")
    capsys.readouterr()
    assert plain_diff(root) == 0
    out = capsys.readouterr().out
    assert "meizel: rules +0 / content +0" in out
    assert "no content growth" in out


EDGE = {
    "meizel.toml": '[roles]\napplication = ["app", "lib"]\ntooling = ["meizel"]\n',
    "lib/__init__.py": "",
    "lib/mod.py": "_hidden = 1\n",
    "lib/meizel.toml": 'exports = ["mod.py"]\n',
    "app/__init__.py": "",
    "app/x.py": "from lib.mod import _hidden\n\nVALUE = _hidden\n",
    "app/meizel.toml": 'exports = ["x.py"]\n',
    "meizel/rule.py": "RULE = 1\n",
}


def test_an_edge_debt_of_a_renamed_file_is_measured_where_the_change_puts_it(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(EDGE)
    assert run(root, "debt", "record", ".") == 0
    write(root, {})
    commit(root)
    git(root, "mv", "app/x.py", "app/y.py")
    write(root, {"app/meizel.toml": 'exports = ["y.py"]\n'})
    assert run(root, "debt", "carry", "--base", "HEAD") == 0
    write(root, {"meizel/rule.py": "RULE = 2\n"})
    capsys.readouterr()
    assert plain_diff(root) == 0
    out = capsys.readouterr().out
    assert "meizel: rules +0 / content +0" in out


ROOTED = {
    "meizel.toml": '[roles]\napplication = ["app"]\ntooling = ["meizel"]\n',
    "app/__init__.py": "",
    "app/daemon.py": "import os\n\nVALUE = os.environ['A']\n",
    "app/meizel.toml": '[roots]\n"daemon.py" = { kinds = ["environ"], why = "the process entry" }\n',
    "meizel/rule.py": "RULE = 1\n",
}


def test_a_declaration_that_follows_a_rename_is_still_a_rule_change(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(ROOTED)
    commit(root)
    git(root, "mv", "app/daemon.py", "app/worker.py")
    write(
        root,
        {
            "app/meizel.toml": '[roots]\n"worker.py" = { kinds = ["environ"], why = "the process entry" }\n',
            "meizel/rule.py": "RULE = 2\n",
        },
    )
    git(root, "add", "-A")
    capsys.readouterr()
    assert plain_diff(root) == 0
    out = capsys.readouterr().out
    assert "meizel: rules +0 / content +0" in out


# meiva:note
# Git's own rename pair for a working tree that has one: the fixture below is only a boundary test
# while git still reads the move as a rename, and a test that silently stopped covering it would look
# green.
def renamed(root: Path) -> list[tuple[str, str]]:
    listing = subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "diff", "-M", "--name-status", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    pairs: list[tuple[str, str]] = []
    for line in listing.splitlines():
        status, _, rest = line.partition("\t")
        if status.startswith("R"):
            old, _, new = rest.partition("\t")
            pairs.append((old, new))
    return pairs


# meiva:note
# The device's own bytes are the change's, and the content's are the reference's: a file the change
# moves across that boundary must not carry the other side's copy into the replay.
INTO_DEVICE = {
    "meizel.toml": '[roles]\napplication = ["app"]\ntooling = ["meizel"]\n',
    "app/__init__.py": "",
    "app/h.py": "def one() -> int:\n    return 1\n\n\ndef two() -> int:\n    return 2\n\n\n"
    "def three() -> int:\n    return 3\n\n\ndef four() -> int:\n    return 4\n",
    "meizel/rule.py": "RULE = 1\n",
}


def test_a_file_moved_into_the_rule_code_runs_the_change_s_own_bytes(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(INTO_DEVICE)
    commit(root)
    git(root, "mv", "app/h.py", "meizel/h.py")
    write(
        root,
        {
            "meizel/h.py": "import os\n\n\ndef one() -> int:\n    return int(os.environ['A'])\n\n\n"
            "def two() -> int:\n    return 2\n\n\ndef three() -> int:\n    return 3\n\n\n"
            "def four() -> int:\n    return 4\n"
        },
    )
    assert ("app/h.py", "meizel/h.py") in renamed(root)
    assert run(root, "debt", "record", ".") == 0
    write(root, {})
    capsys.readouterr()
    assert plain_diff(root) == 0
    out = capsys.readouterr().out
    assert "meizel: rules +1 / content +0" in out


OUT_OF_DEVICE = {
    "meizel.toml": '[roles]\napplication = ["app"]\ntooling = ["meizel"]\n',
    "app/__init__.py": "",
    "app/keep.py": "",
    "app/meizel.toml": 'exports = ["keep.py"]\n',
    "meizel/meizel.toml": '[debt.ambient-read]\n"h.py#environ os.environ" = 1\n',
    "meizel/rule.py": "RULE = 1\n",
    "meizel/h.py": "import os\n\nVALUE = os.environ['A']\n",
}


def test_a_file_moved_out_of_the_rule_code_is_measured_as_content(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(OUT_OF_DEVICE)
    commit(root)
    git(root, "mv", "meizel/h.py", "app/h.py")
    write(root, {"meizel/rule.py": "RULE = 2\n"})
    assert run(root, "debt", "carry", "--base", "HEAD") == 0
    git(root, "add", "-A")
    capsys.readouterr()
    assert plain_diff(root) == 0
    out = capsys.readouterr().out
    assert "meizel: rules +0 / content +0" in out


def test_a_read_added_while_the_file_moves_is_content_growth(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(DECLARED)
    commit(root)
    write(root, {"meizel/rule.py": "RULE = 2\n"})
    git(root, "mv", "app/daemon.py", "app/worker.py")
    assert run(root, "debt", "carry", "--base", "HEAD") == 0
    write(
        root, {"app/worker.py": "import os\n\nvalue = os.environ['A']\nother = os.environ['B']\n"}
    )
    assert run(root, "debt", "record", ".") == 0
    write(root, {})
    capsys.readouterr()
    assert plain_diff(root) == 1
    out = capsys.readouterr().out
    assert "worker.py#environ os.environ (ambient-read) 2 recorded, 1 found" in out


# meiva:note
# Most files live under an ancestor manifest, not one of their own: a rename into such a directory is
# still measured at the path the change gives it, against the manifest that owns it there.
NESTED = {
    "meizel.toml": '[roles]\napplication = ["app"]\ntooling = ["meizel"]\n',
    "app/__init__.py": "",
    "app/sub/keep.py": "",
    "app/x.py": "import os\n\nVALUE = os.environ['A']\n",
    "app/meizel.toml": '[debt.ambient-read]\n"x.py#environ os.environ" = 1\n',
    "meizel/rule.py": "RULE = 1\n",
}


def test_a_rename_into_a_directory_without_a_manifest_is_not_growth(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(NESTED)
    commit(root)
    git(root, "mv", "app/x.py", "app/sub/x.py")
    write(root, {"meizel/rule.py": "RULE = 2\n"})
    assert run(root, "debt", "carry", "--base", "HEAD") == 0
    git(root, "add", "-A")
    capsys.readouterr()
    assert plain_diff(root) == 0
    out = capsys.readouterr().out
    assert "meizel: rules +0 / content +0" in out
