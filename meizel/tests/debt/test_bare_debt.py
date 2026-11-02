from __future__ import annotations

from pathlib import Path

import pytest

from meizel.graph.model import Diagnostic
from meizel.tests.synthetic import RepoFactory, analysis_of, run

USE = {
    "pkg/__init__.py": "",
    "pkg/a.py": "from pkg.b import _secret\n",
    "pkg/b.py": "_secret = 1\n",
}
DECLARED = '[uses]\n"a.py" = ["b.py"]\n'
KEY = "a.py -> b.py#_secret"
COMPLETE = '{ count = 1, owner = "#6018", until = "permanently: the fixture reads it" }'


# meiva:note
# A manifest whose `[debt.private-name]` table holds these entries, one per line: the fixture every
# test here reads, so a test says only what each entry carries.
def manifest(**entries: str) -> str:
    rows = "\n".join(f'"{key}" = {value}' for key, value in entries.items())
    return f"{DECLARED}\n[debt.private-name]\n{rows}\n"


# meiva:note
# The code under test, as a plain string: the verdict's `code` is compared by value, so a reader greps
# the same word the output prints.
BARE: str = "bare-debt"


def bare(root: Path) -> list[Diagnostic]:
    return [d for d in analysis_of(root).verdict if d.code.value == BARE]


def test_a_complete_entry_is_not_reported(make_repo: RepoFactory) -> None:
    root = make_repo({**USE, "pkg/meizel.toml": manifest(**{KEY: COMPLETE})})
    assert analysis_of(root).verdict == []
    assert run(root, "check") == 0


def test_a_bare_entry_is_reported_once_at_its_own_line(make_repo: RepoFactory) -> None:
    root = make_repo({**USE, "pkg/meizel.toml": manifest(**{KEY: "1"})})
    [only] = bare(root)
    assert (only.path, only.line) == ("pkg/meizel.toml", 5)
    assert "records debt with no owner and no exit condition" in only.message
    assert "`owner` and `until` are missing" in only.message
    assert run(root, "check") == 1


def test_the_full_text_reaches_the_first_entry_of_each_kind_only(make_repo: RepoFactory) -> None:
    entries = {
        "a.py -> b.py#_until": '{ count = 1, owner = "synthetic" }',
        "a.py -> b.py#_owner": '{ count = 1, until = "permanently: a fixture entry" }',
        "a.py -> b.py#_both": "1",
    }
    root = make_repo({**USE, "pkg/meizel.toml": manifest(**entries)})
    found = bare(root)
    assert [d.line for d in found] == [5, 6, 7]
    owner_text, until_text = "`owner` is who re-reads the entry", "`until` answers one question"
    everything = "\n".join(d.render() for d in found)
    assert everything.count(owner_text) == 1
    assert everything.count(until_text) == 1
    assert everything.count("These are errors, not warnings") == 1
    assert until_text in found[0].render()
    assert "These are errors, not warnings" in found[0].render()
    assert owner_text in found[1].render()
    assert "`until` answers one question" not in found[1].render()
    assert "\n" not in found[2].render()


def test_twenty_bare_entries_print_one_line_each_after_the_first(make_repo: RepoFactory) -> None:
    entries = {"a.py -> b.py#_both": "1"}
    entries.update(
        {
            f"a.py -> b.py#_until_{n}": COMPLETE.split(", until", maxsplit=1)[0] + " }"
            for n in range(19)
        }
    )
    root = make_repo({**USE, "pkg/meizel.toml": manifest(**entries)})
    found = bare(root)
    assert len(found) == 20
    assert found[0].line == 5
    assert "`until` answers one question" in found[0].render()
    assert all("\n" not in d.render() for d in found[1:])
    assert sum(len(d.render().splitlines()) for d in found) < 60


def test_an_until_in_no_shape_is_an_error_that_names_both(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    entries = {
        KEY: '{ count = 1, owner = "synthetic", until = "the same seam as the entry above" }'
    }
    root = make_repo({**USE, "pkg/meizel.toml": manifest(**entries)})
    [only] = bare(root)
    assert "states an exit condition in no shape this tool reads" in only.message
    assert "`waits for ...:` or `permanently:`" in only.message
    assert "Something will arrive" in only.render()
    assert run(root, "check") == 1


def test_a_manifest_that_keeps_its_metadata_is_the_only_clean_one(make_repo: RepoFactory) -> None:
    root = make_repo({**USE, "pkg/meizel.toml": manifest(**{KEY: COMPLETE})})
    assert bare(root) == []
    assert analysis_of(root).diagnostics, "the violation is still found; only the verdict allows it"
