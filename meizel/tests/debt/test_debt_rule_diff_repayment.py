# meiva:note
# The third number `debt diff` prints, and the pair that makes it necessary: the same root declaration,
# once with the reads it hid still in the code and once with them deleted. `content` prints both
# identically — deleting a read the new device no longer measures moves neither of its two terms — so a
# reader of the two old columns cannot tell a repayment from a declaration that stopped measuring.
# `repaid` holds the device where the ref left it and counts what the ref's own declarations no longer
# find in the change's code.

from __future__ import annotations

from pathlib import Path

import pytest

from meizel.tests.synthetic import RepoFactory, commit, run, write

RULES = ":(glob)rules/**"

# meiva:note
# One environment read, recorded, in a file the change can either declare a root or stop reading. The
# rule code is a fixture file of this module (`rules/`), so `diff` names it for the caller: a change to
# it is a rule change only when the caller says so.
DECLARED_BASE = {
    "meizel.toml": '[roles]\napplication = ["app"]\ntooling = ["rules"]\n',
    "app/__init__.py": "",
    "app/daemon.py": "import os\n\nvalue = os.environ['A']\n",
    "app/meizel.toml": '[debt.ambient-read]\n"daemon.py#environ os.environ" = 1\n',
    "rules/engine.py": "RULE = 1\n",
}
ROOTED = '[roots]\n"daemon.py" = { kinds = ["environ"], why = "the process entry" }\n'


# meiva:note
# `debt diff` without extra rule pathspecs: the declaration is what it detects, so the two columns are
# printed on its own.
def plain_diff(root: Path, on_growth: str = "fail") -> int:
    return run(root, "debt", "diff", "--against", "HEAD", "--on-growth", on_growth)


def diff(root: Path, on_growth: str = "fail") -> int:
    return run(
        root,
        "debt",
        "diff",
        "--against",
        "HEAD",
        "--on-growth",
        on_growth,
        "--waive-if-changed",
        RULES,
    )


def rule_change(root: Path) -> None:
    write(root, {"rules/engine.py": "RULE = 2\n"})


# meiva:note
# No line of code changes: the read leaves the record because the device stopped measuring it.
def test_a_root_declaration_is_a_rule_change_and_not_repayment(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(DECLARED_BASE)
    commit(root)
    write(root, {"app/meizel.toml": ROOTED})
    assert run(root, "debt", "prune") == 0
    write(root, {})
    capsys.readouterr()
    assert plain_diff(root) == 0
    out = capsys.readouterr().out
    row = next(line.split() for line in out.splitlines() if line.startswith("ambient-read"))
    assert row[1:] == ["1", "0", "0", "-1", "+0", "+0"]
    assert "meizel: rules -1 / content +0 / repaid +0 against HEAD" in out
    assert "what this change edits in the measurement" in out
    assert "app/meizel.toml [roots] daemon.py +" in out


# meiva:note
# The other half of the pair, and the reason `repaid` exists: the same declaration, with the reads it
# hides really deleted. Pull request 8369 was this case — two `sys.executable` reads deleted from
# `bisect_train.py` and its `[roots]` given `pid`, reported as `rules -2 / content +0` alone.
def test_a_root_declared_over_deleted_reads_is_a_repayment(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(DECLARED_BASE)
    commit(root)
    write(root, {"app/daemon.py": "", "app/meizel.toml": ROOTED})
    capsys.readouterr()
    assert plain_diff(root) == 0
    out = capsys.readouterr().out
    row = next(line.split() for line in out.splitlines() if line.startswith("ambient-read"))
    assert row[1:] == ["1", "0", "0", "-1", "+0", "+1"]
    assert "meizel: rules -1 / content +0 / repaid +1 against HEAD" in out


# meiva:note
# The other direction: the change's code does something the ref's device still measures, so the third
# column is negative and names the growth `content` would otherwise leave to the rules.
def test_a_read_the_old_rules_measure_arriving_under_a_rule_change_is_negative_repaid(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(DECLARED_BASE)
    commit(root)
    rule_change(root)
    write(root, {"app/other.py": "import os\n\nvalue = os.environ['B']\n"})
    assert (
        run(
            root, "debt", "record", ".", "--owner", "synthetic", "--until", "permanently: a fixture"
        )
        == 0
    )
    write(root, {})
    capsys.readouterr()
    assert diff(root) == 1
    out = capsys.readouterr().out
    row = next(line.split() for line in out.splitlines() if line.startswith("ambient-read"))
    assert row[1:] == ["1", "1", "2", "+0", "+1", "-1"]
    assert "meizel: rules +0 / content +1 / repaid -1 against HEAD" in out
