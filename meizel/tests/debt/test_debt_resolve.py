# meiva:note
# `debt resolve`: a merge conflict inside the recorded-debt tables is resolved, anywhere else it is refused.

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from meizel.tests.synthetic import RepoFactory, commit, run, write

BASE = {
    "lib/__init__.py": "",
    "lib/_hidden.py": "",
    "app/__init__.py": "",
    "app/x.py": "import lib._hidden\nimport lib._hidden\nimport lib._hidden\n",
    "app/y.py": "import lib._hidden\n",
}
X = "app/x.py -> lib/_hidden.py"
Y = "app/y.py -> lib/_hidden.py"
TABLE = "[debt.private-module]\n"


def conflict(ours: str, theirs: str, base: str | None = None) -> str:
    middle = f"||||||| base\n{base}" if base is not None else ""
    return f"<<<<<<< ours\n{ours}{middle}=======\n{theirs}>>>>>>> theirs\n"


def entry(key: str, count: int) -> str:
    return f'"{key}" = {count}\n'


def recorded_repo(make_repo: RepoFactory) -> Path:
    root = make_repo(BASE)
    assert run(root, "sync", "--declare", ".") in {0, 1}
    write(root, {})
    assert run(root, "debt", "record", ".") == 0
    write(root, {})
    commit(root)
    return root


def debts(root: Path) -> dict[str, dict[str, int]]:
    return tomllib.loads((root / "meizel.toml").read_text()).get("debt", {})


# meiva:note
# The root manifest with its `private-module` table replaced by `middle`.
def conflicted(root: Path, middle: str) -> None:
    text = (root / "meizel.toml").read_text()
    head, _, rest = text.partition(TABLE)
    tail = rest.split("\n\n", 1)[1] if "\n\n" in rest else ""
    write(root, {"meizel.toml": head + TABLE + middle + ("\n" + tail if tail else "")})


# meiva:note
# The tree still has three occurrences: a violation neither side recorded is a decision for `debt record`,
# not a number to raise.
def test_a_count_both_sides_changed_becomes_the_lower_one_and_is_never_raised(
    make_repo: RepoFactory,
) -> None:
    root = recorded_repo(make_repo)
    conflicted(root, conflict(entry(X, 3) + entry(Y, 1), entry(X, 2) + entry(Y, 1)))
    assert run(root, "debt", "resolve", "meizel.toml") == 0
    assert debts(root)["private-module"] == {X: 2, Y: 1}
    assert run(root, "check") == 1


def test_the_entries_only_one_side_holds_are_kept(make_repo: RepoFactory) -> None:
    root = recorded_repo(make_repo)
    conflicted(root, conflict(entry(X, 3), entry(Y, 1)))
    assert run(root, "debt", "resolve", "meizel.toml") == 0
    assert debts(root)["private-module"] == {X: 3, Y: 1}
    assert run(root, "check") == 0


def test_a_base_section_of_diff3_style_markers_is_ignored(make_repo: RepoFactory) -> None:
    root = recorded_repo(make_repo)
    conflicted(
        root,
        conflict(
            entry(X, 3) + entry(Y, 1), entry(X, 2) + entry(Y, 1), base=entry(X, 1) + entry(Y, 1)
        ),
    )
    assert run(root, "debt", "resolve", "meizel.toml") == 0
    assert debts(root)["private-module"] == {X: 2, Y: 1}


def test_entries_after_a_hunk_that_holds_the_header_stay_in_their_table(
    make_repo: RepoFactory,
) -> None:
    root = recorded_repo(make_repo)
    kept = debts(root)["undeclared-edge"]
    text = (root / "meizel.toml").read_text()
    head = text.partition(TABLE)[0]
    hunk = conflict(TABLE + entry(X, 3), TABLE + entry(X, 3))
    other = "\n[debt.undeclared-edge]\n" + "".join(entry(k, n) for k, n in kept.items())
    write(root, {"meizel.toml": head + hunk + entry(Y, 1) + other})
    assert run(root, "debt", "resolve", "meizel.toml") == 0
    assert debts(root)["private-module"] == {X: 3, Y: 1}
    assert debts(root)["undeclared-edge"] == kept
    assert run(root, "check") == 0


@pytest.mark.parametrize("section", ["purity", "roots", "uses", "state", "exports"])
def test_a_conflict_outside_the_debt_tables_is_left_for_a_person(
    make_repo: RepoFactory, section: str
) -> None:
    root = recorded_repo(make_repo)
    text = f"[{section}]\n" + conflict('"a.py" = "x"\n', '"a.py" = "y"\n')
    write(root, {"lib/meizel.toml": text})
    assert run(root, "debt", "resolve", "lib/meizel.toml") == 2
    assert (root / "lib" / "meizel.toml").read_text() == text


def test_a_conflict_that_mixes_a_debt_table_with_another_section_is_refused(
    make_repo: RepoFactory,
) -> None:
    root = recorded_repo(make_repo)
    text = conflict(TABLE + entry(X, 1) + '[purity]\n"a.py" = "strict"\n', "")
    write(root, {"lib/meizel.toml": text})
    assert run(root, "debt", "resolve", "lib/meizel.toml") == 2


def test_a_table_name_that_cannot_be_recorded_is_refused(make_repo: RepoFactory) -> None:
    root = recorded_repo(make_repo)
    text = "[debt.not-a-code]\n" + conflict(entry("a", 1), entry("a", 2))
    write(root, {"lib/meizel.toml": text})
    assert run(root, "debt", "resolve", "lib/meizel.toml") == 2


def test_a_manifest_without_markers_is_not_rewritten_by_the_resolver(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = recorded_repo(make_repo)
    text = (root / "meizel.toml").read_text()
    unsorted = text.replace(entry(X, 3) + entry(Y, 1), entry(Y, 1) + entry(X, 3))
    assert unsorted != text
    write(root, {"meizel.toml": unsorted})
    capsys.readouterr()
    assert run(root, "debt", "resolve", "meizel.toml") == 0
    assert "resolved" not in capsys.readouterr().out


def test_one_refused_manifest_leaves_every_file_as_it_was(make_repo: RepoFactory) -> None:
    root = recorded_repo(make_repo)
    bad = "[purity]\n" + conflict('"x.py" = "strict"\n', '"x.py" = "strict-to-be"\n')
    conflicted(root, conflict(entry(X, 3), entry(X, 2)))
    write(root, {"lib/meizel.toml": bad})
    before = {p: (root / p).read_text() for p in ("meizel.toml", "lib/meizel.toml")}
    assert run(root, "debt", "resolve", "meizel.toml", "lib/meizel.toml") == 2
    assert {p: (root / p).read_text() for p in before} == before


def test_a_path_that_is_not_a_manifest_is_refused(make_repo: RepoFactory) -> None:
    root = recorded_repo(make_repo)
    assert run(root, "debt", "resolve", "app/x.py") == 2


def test_the_command_lowers_the_resolved_tables_to_what_occurs(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = recorded_repo(make_repo)
    conflicted(root, conflict(entry(X, 9) + entry(Y, 9), entry(X, 8) + entry(Y, 9)))
    capsys.readouterr()
    assert run(root, "debt", "resolve", "meizel.toml") == 0
    assert "meizel: resolved meizel.toml" in capsys.readouterr().out
    assert "<<<<<<<" not in (root / "meizel.toml").read_text()
    assert debts(root)["private-module"] == {X: 3, Y: 1}
    assert run(root, "check") == 0
