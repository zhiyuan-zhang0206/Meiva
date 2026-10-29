# meiva:note
# One recorded debt's count, owner and condition: both spellings read alike, and the entries with no
# owner are countable and listable instead of hidden among the rest.

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from meizel.contracts.analysis import DebtEntry, entry_value, read_entry, set_debt
from meizel.graph.model import Code
from meizel.tests.synthetic import RepoFactory, commit, failures, git, run, write

DECLARED = '[uses]\n"a.py" = ["b.py"]\n'
BASE = {
    "pkg/__init__.py": "",
    "pkg/a.py": "from pkg.b import _secret\n",
    "pkg/b.py": "_secret = 1\n",
    "pkg/meizel.toml": DECLARED,
}
TWO_READS = {
    **BASE,
    "pkg/a.py": "from pkg.b import _secret, _other\n",
    "pkg/b.py": "_secret = 1\n_other = 2\n",
}


def record(root: Path, *paths: str) -> int:
    code = run(root, "debt", "record", *paths)
    git(root, "add", "-A")
    return code


def table_of(root: Path, code: str) -> dict[str, object]:
    text = (root / "pkg" / "meizel.toml").read_text()
    return tomllib.loads(text)["debt"][code]


def test_a_bare_count_and_a_named_entry_read_as_the_same_value() -> None:
    reported: list[str] = []
    named = read_entry(
        {"count": 3, "owner": "#7139", "until": "waits for the Settings port"}, "x", reported.append
    )
    assert named is not None
    assert named == DebtEntry(3, "#7139", "waits for the Settings port")
    assert read_entry(3, "x", reported.append) == DebtEntry(3)
    assert reported == []
    assert entry_value(named) == {
        "count": 3,
        "owner": "#7139",
        "until": "waits for the Settings port",
    }
    assert entry_value(DebtEntry(3)) == 3


@pytest.mark.parametrize(
    "value",
    [{"count": 0}, {"count": 1, "owner": ""}, {"count": 1, "ownr": "#7139"}, {"count": True}],
)
def test_a_malformed_entry_is_reported_rather_than_dropped(value: object) -> None:
    reported: list[str] = []
    assert read_entry(value, "x", reported.append) is None
    assert reported


def test_the_written_form_reads_back_as_the_same_entry() -> None:
    entry = DebtEntry(2, "#7139", "waits for the Settings port")
    written = set_debt("", {Code.PRIVATE_NAME: {"a.py -> b.py#_c": entry}})
    assert tomllib.loads(written)["debt"]["private-name"]["a.py -> b.py#_c"] == entry_value(entry)


def test_a_manifest_debt_with_a_malformed_entry_fails(make_repo: RepoFactory) -> None:
    root = make_repo(BASE)
    write(
        root,
        {
            "pkg/meizel.toml": DECLARED + "\n[debt.private-name]\n"
            '"a.py -> b.py#_secret" = { count = 1, ownr = "#7139" }\n'
        },
    )
    assert ("manifest-debt", "pkg/meizel.toml") in failures(root)


def test_recording_again_keeps_the_owner_it_already_had(make_repo: RepoFactory) -> None:
    root = make_repo(BASE)
    write(
        root,
        {
            "pkg/meizel.toml": DECLARED + "\n[debt.private-name]\n"
            '"a.py -> b.py#_secret" = { count = 1, owner = "#7139", until = "waits for the door" }\n'
        },
    )
    commit(root)
    assert record(root, "pkg") == 0
    assert table_of(root, "private-name") == {
        "a.py -> b.py#_secret": {
            "count": 1,
            "owner": "#7139",
            "until": "waits for the door",
        }
    }


def test_pruning_lowers_the_count_and_keeps_the_owner(make_repo: RepoFactory) -> None:
    root = make_repo(BASE)
    write(
        root,
        {
            "pkg/meizel.toml": DECLARED + "\n[debt.private-name]\n"
            '"a.py -> b.py#_secret" = { count = 2, owner = "#7139" }\n'
        },
    )
    commit(root)
    assert run(root, "debt", "prune") == 0
    git(root, "add", "-A")
    assert table_of(root, "private-name") == {
        "a.py -> b.py#_secret": {"count": 1, "owner": "#7139"}
    }


def test_carrying_a_rename_keeps_the_owner(make_repo: RepoFactory) -> None:
    root = make_repo(BASE)
    write(
        root,
        {
            "pkg/meizel.toml": DECLARED + "\n[debt.private-name]\n"
            '"a.py -> b.py#_secret" = { count = 1, owner = "#7139", until = "waits for the door" }\n'
        },
    )
    commit(root)
    git(root, "mv", "pkg/a.py", "pkg/moved.py")
    assert run(root, "debt", "carry", "--base", "HEAD") == 0
    git(root, "add", "-A")
    assert table_of(root, "private-name") == {
        "moved.py -> b.py#_secret": {
            "count": 1,
            "owner": "#7139",
            "until": "waits for the door",
        }
    }


# meiva:note
# A move that lands on a key its new directory already records merges the two entries: the counts add,
# and the owner the moved entry names survives, so following a rename never quietly drops who pays.
def test_carrying_onto_a_recorded_key_keeps_the_moved_owner(make_repo: RepoFactory) -> None:
    root = make_repo(BASE)
    write(
        root,
        {
            "pkg/meizel.toml": DECLARED + "\n[debt.private-name]\n"
            '"a.py -> b.py#_secret" = { count = 1, owner = "#7139" }\n'
            '"moved.py -> b.py#_secret" = 1\n'
        },
    )
    commit(root)
    git(root, "mv", "pkg/a.py", "pkg/moved.py")
    assert run(root, "debt", "carry", "--base", "HEAD") == 0
    git(root, "add", "-A")
    assert table_of(root, "private-name") == {
        "moved.py -> b.py#_secret": {"count": 2, "owner": "#7139"}
    }


# meiva:note
# The summary says how many recorded debts name no owner, and `--unowned` appends the list at the end —
# as the detail of the table above it, not between that table's header and its rows.
def test_the_summary_counts_and_lists_the_entries_without_an_owner(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(TWO_READS)
    write(
        root,
        {
            "pkg/meizel.toml": DECLARED + "\n[debt.private-name]\n"
            '"a.py -> b.py#_secret" = { count = 1, owner = "#7139" }\n'
            '"a.py -> b.py#_other" = 1\n'
        },
    )
    commit(root)
    capsys.readouterr()
    assert run(root, "debt", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["unowned"] == {"entries": 1, "occurrences": 1}
    capsys.readouterr()
    assert run(root, "debt", "--unowned") == 0
    out = capsys.readouterr().out
    listed = "pkg/meizel.toml: private-name a.py -> b.py#_other (1)"
    assert listed in out
    assert "a.py -> b.py#_secret (1)" not in out
    rows, _, detail = out.split("by top-level directory:", 1)[1].partition("\n\n")
    assert "in " in rows
    assert listed in detail


def test_the_check_line_names_the_entries_without_an_owner(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(TWO_READS)
    write(
        root,
        {
            "pkg/meizel.toml": DECLARED + "\n[debt.private-name]\n"
            '"a.py -> b.py#_secret" = { count = 1, owner = "#7139" }\n'
            '"a.py -> b.py#_other" = 1\n'
        },
    )
    commit(root)
    capsys.readouterr()
    assert run(root, "check") == 0
    assert "1 entry (1 occurrence) names no owner" in capsys.readouterr().out


# meiva:note
# `--unowned` is the summary's flag like `--json` is: a subcommand that never reads it says so instead
# of printing a report that silently ignores the request.
def test_unowned_with_a_subcommand_is_refused(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(BASE)
    capsys.readouterr()
    assert run(root, "debt", "--unowned", "prune") != 0
    assert "--unowned belongs to the summary" in capsys.readouterr().err


# meiva:note
# A move that lands on an entry somebody already owns is a real disagreement about who pays: the entry
# already there keeps it, and the command says so rather than merging the two owners into one.
def test_carrying_onto_another_owner_reports_which_one_it_kept(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(BASE)
    write(
        root,
        {
            "pkg/meizel.toml": DECLARED + "\n[debt.private-name]\n"
            '"a.py -> b.py#_secret" = { count = 1, owner = "#7139" }\n'
            '"moved.py -> b.py#_secret" = { count = 1, owner = "#4242" }\n'
        },
    )
    commit(root)
    git(root, "mv", "pkg/a.py", "pkg/moved.py")
    capsys.readouterr()
    assert run(root, "debt", "carry", "--base", "HEAD") == 0
    assert "kept owner #4242 over #7139" in capsys.readouterr().out
    git(root, "add", "-A")
    assert table_of(root, "private-name") == {
        "moved.py -> b.py#_secret": {"count": 2, "owner": "#4242"}
    }
