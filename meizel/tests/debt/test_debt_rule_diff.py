"""`debt diff` for a change that edits the rules: the growth the rules cause is reported, the content's is judged."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from meizel.tests.synthetic import RepoFactory, commit, run, write

RULES = ":(glob)rules/**"
# `app` imports through an underscore module: a violation the base's old rules did not record, and one
# that no declaration can fix, so a rule change must record it.
BASE = {
    "lib/__init__.py": "",
    "lib/_hidden.py": "",
    "app/__init__.py": "",
    "app/x.py": "import lib._hidden\n",
    "rules/engine.py": "RULE = 1\n",
}


def base_repo(make_repo: RepoFactory, extra: dict[str, str] | None = None) -> Path:
    root = make_repo({**BASE, **(extra or {})})
    commit(root)
    return root


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


def record(root: Path) -> None:
    """What a rule change's baseline commit does: declare the legal edges, record the rest, stage."""
    run(root, "sync", "--declare", ".")  # exits 1 while an error is left for `record`
    write(root, {})
    assert run(root, "debt", "record", ".") == 0
    write(root, {})


def rule_change(root: Path) -> None:
    write(root, {"rules/engine.py": "RULE = 2\n"})


def test_a_pure_rule_change_may_record_what_the_new_rules_find(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = base_repo(make_repo)
    rule_change(root)
    record(root)
    capsys.readouterr()
    assert diff(root) == 0
    out = capsys.readouterr().out
    assert "private-module" in out
    assert "+1" in out
    assert "  rules/engine.py" in out
    assert "no content growth" in out


# meiva:note
# A manifest that declares a file the change adds: the declaration is written over the base's content
# too, where that file does not exist, so it names a child the base cannot hold. It is dropped there
# instead of read as a broken base (pull request 7625: 7600 and 7620 each added a file to a declared
# directory, and the batch was refused before anything was measured).
def test_a_file_added_to_a_declared_directory_is_measured_not_refused(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = base_repo(make_repo, {"app/meizel.toml": 'exports = ["x.py"]\n'})
    write(root, {"app/z.py": "", "app/meizel.toml": 'exports = ["x.py", "z.py"]\n'})
    capsys.readouterr()
    assert diff(root) == 0
    out = capsys.readouterr().out
    assert "no content growth" in out
    assert "app/meizel.toml [exports] * ~" in out


# meiva:note
# The declaration that names nothing stays the change's own violation. Dropping it from the base's
# baseline is not a way past the gate: `check` fails on the tree that declares it.
def test_a_manifest_naming_a_file_no_tree_has_stays_a_violation(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = base_repo(make_repo, {"app/meizel.toml": 'exports = ["x.py"]\n'})
    write(root, {"app/meizel.toml": 'exports = ["x.py", "ghost.py"]\n'})
    capsys.readouterr()
    assert run(root, "check") == 1
    assert "manifest-unknown-child" in capsys.readouterr().out


def test_a_rule_change_that_also_adds_a_violation_fails(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = base_repo(make_repo)
    rule_change(root)
    write(root, {"app/y.py": "import lib._hidden\n"})
    record(root)
    capsys.readouterr()
    assert diff(root) == 1
    out = capsys.readouterr()
    assert "app/y.py" in out.out
    assert "grew" in out.err


def test_a_rule_change_cannot_replace_one_debt_with_another(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = base_repo(make_repo)
    rule_change(root)
    write(root, {"app/x.py": "", "app/y.py": "import lib._hidden\n"})
    record(root)
    capsys.readouterr()
    assert diff(root) == 1
    out = capsys.readouterr()
    assert "app/y.py" in out.out
    assert "1 recorded, 0 found" in out.out
    assert "grew" in out.err


def test_a_rule_change_cannot_increase_an_existing_debt_count(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = base_repo(make_repo)
    rule_change(root)
    write(root, {"app/x.py": "import lib._hidden\nimport lib._hidden\n"})
    record(root)
    capsys.readouterr()
    assert diff(root, on_growth="warn") == 0
    out = capsys.readouterr()
    assert "app/x.py" in out.out
    assert "2 recorded, 1 found" in out.out
    assert "warning" in out.err


# meiva:note
# `debt diff` without the extra rule pathspecs: it has to detect the rule change by itself.
def plain_diff(root: Path, on_growth: str = "fail") -> int:
    return run(root, "debt", "diff", "--against", "HEAD", "--on-growth", on_growth)


def test_editing_only_the_debt_tables_keeps_the_single_column(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(DECLARED_BASE)
    commit(root)
    write(
        root,
        {
            "app/meizel.toml": "# what the device measures here\n\n[debt.ambient-read]\n"
            '"daemon.py#environ os.environ" = 1\n'
        },
    )
    capsys.readouterr()
    assert plain_diff(root) == 0
    out = capsys.readouterr().out
    assert "rules " not in out
    assert "no growth" in out


# meiva:note
# A policy the change edits without touching an entry (a comment) is still part of the device: the
# report names the file, so the two measures are never printed without an input that explains them.
def test_a_policy_edit_with_no_entry_change_still_names_the_policy(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(DECLARED_BASE)
    commit(root)
    policy = (root / "meizel.policy.toml").read_text(encoding="utf-8")
    write(root, {"meizel.policy.toml": policy + "# a note the entries do not see\n"})
    capsys.readouterr()
    assert plain_diff(root) == 0
    out = capsys.readouterr().out
    assert "meizel: rules +0 / content +0" in out
    assert "  meizel.policy.toml" in out


# meiva:note
# The tests under `meizel/` are not the device either: the checker records nothing there, and editing
# one keeps the totals-only report instead of spending a second column on it.
def test_editing_only_a_test_file_under_meizel_keeps_the_single_column(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(
        {
            **DECLARED_BASE,
            "meizel.toml": '[roles]\napplication = ["app"]\ntooling = ["meizel", "rules"]\n',
            "meizel/rule.py": "RULE = 1\n",
            "meizel/tests/debt/probe.py": "VALUE = 1\n",
        }
    )
    commit(root)
    write(root, {"meizel/tests/debt/probe.py": "VALUE = 2\n"})
    capsys.readouterr()
    assert plain_diff(root) == 0
    out = capsys.readouterr().out
    assert "rules " not in out
    assert "no growth" in out


def test_a_rule_code_change_under_meizel_is_detected_without_a_flag(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(
        {
            **DECLARED_BASE,
            "meizel.toml": '[roles]\napplication = ["app"]\ntooling = ["meizel", "rules"]\n',
            "meizel/rule.py": "RULE = 1\n",
        }
    )
    commit(root)
    write(root, {"meizel/rule.py": "RULE = 2\n"})
    capsys.readouterr()
    assert plain_diff(root) == 0
    out = capsys.readouterr().out
    assert "meizel: rules +0 / content +0" in out
    assert "what this change edits in the measurement" in out
    assert "  meizel/rule.py" in out


# meiva:note
# Only the rule code is the device: everything else under `meizel/` is measured as it was, including
# the three allowances readers the rule-code pathspecs exclude by name.
@pytest.mark.parametrize("probe", ["meizel/state/probe.py", "meizel/allowances.py"])
def test_a_violation_the_change_adds_under_meizel_is_content_growth(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str], probe: str
) -> None:
    root = make_repo(
        {
            **DECLARED_BASE,
            "meizel.toml": '[roles]\napplication = ["app"]\ntooling = ["meizel", "rules"]\n',
            "meizel/rule.py": "RULE = 1\n",
            probe: "VALUE = 1\n",
        }
    )
    commit(root)
    write(
        root,
        {
            "meizel/rule.py": "RULE = 2\n",
            probe: "import os\n\nVALUE = os.environ['A']\n",
        },
    )
    assert run(root, "debt", "record", ".") == 0
    write(root, {})
    capsys.readouterr()
    assert plain_diff(root) == 1
    assert f"{probe.rsplit('/', 1)[-1]}#environ os.environ" in capsys.readouterr().out


def test_a_content_change_that_moves_a_file_is_not_growth(make_repo: RepoFactory) -> None:
    root = base_repo(make_repo)
    record(root)
    commit(root)
    write(root, {"app/z.py": "import lib._hidden\n"})
    (root / "app" / "x.py").unlink()
    record(root)
    assert run(root, "debt", "prune") == 0
    write(root, {})
    assert diff(root) == 0


def test_a_content_change_behind_a_rule_change_passes_with_it(make_repo: RepoFactory) -> None:
    root = base_repo(make_repo)
    rule_change(root)
    write(root, {"app/x.py": "import lib._hidden\n\nVALUE = 1\n", "app/notes.md": "text\n"})
    record(root)
    assert diff(root) == 0


def test_a_content_change_with_no_rule_change_is_judged_by_total(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = base_repo(make_repo)
    write(root, {"app/y.py": "import lib._hidden\n"})
    record(root)
    capsys.readouterr()
    assert diff(root) == 1
    assert "grew" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("path", "text"),
    [
        ("app/meizel.toml", 'not_a_key = ["x"]\n'),
        ("app/meizel.toml", "exports = [\n"),
        ("meizel.policy.toml", "not_a_table = []\n"),
    ],
)
def test_manifests_the_checker_cannot_read_stop_the_comparison(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str], path: str, text: str
) -> None:
    root = base_repo(make_repo, {path: text})
    rule_change(root)
    write(root, {path: ""})
    assert diff(root) == 2
    assert "cannot read the manifests" in capsys.readouterr().err


@pytest.mark.parametrize("unreadable", [False, True])
def test_comparison_preserves_the_working_tree_and_removes_its_checkout(
    make_repo: RepoFactory, unreadable: bool
) -> None:
    extra = {"app/meizel.toml": "unknown = []\n"} if unreadable else {}
    root = base_repo(make_repo, extra)
    rule_change(root)
    if unreadable:
        write(root, {"app/meizel.toml": ""})
    else:
        record(root)
    (root / "untracked.txt").write_text("keep this file\n")
    before = {path: path.read_bytes() for path in root.rglob("meizel.toml")}
    assert diff(root) == (2 if unreadable else 0)
    assert {path: path.read_bytes() for path in root.rglob("meizel.toml")} == before
    assert (root / "rules/engine.py").read_text() == "RULE = 2\n"
    assert (root / "untracked.txt").read_text() == "keep this file\n"
    worktrees = subprocess.check_output(  # noqa: S603 - fixed argv
        ["git", "-C", str(root), "worktree", "list", "--porcelain"], text=True
    )
    assert worktrees.count("worktree ") == 1


LEGACY_POLICY = """
[ambient-state]
config-modules = ["base.config"]
config-readers = ["settings"]
db-modules = ["base.db"]
db-primitives = ["connect"]
db-handles = ["base.db:Database"]
bus-handles = ["base.events:EventBus"]
endpoint-handles = ["base.endpoints:ServiceEndpoints"]
[no-os-environ.files]
"app/env.py" = "the process environment boundary"
"""
AMBIENT_POLICY = """
[ambient]
settings = ["settings"]
db = ["connect()", "Database.from_settings()"]
bus = ["EventBus.from_settings()"]
endpoints = ["ServiceEndpoints.from_settings()"]
"""


def ambient_base(make_repo: RepoFactory, policy: str = LEGACY_POLICY) -> Path:
    return base_repo(
        make_repo,
        {
            "meizel.policy.toml": policy,
            "meizel.toml": '[roles]\napplication = ["app", "base", "lib"]\ntooling = ["rules"]\n',
            "base/__init__.py": "",
            "base/config.py": "settings = None\n",
            "base/db.py": "def connect(): ...\nclass Database: ...\n",
            "base/events.py": "class EventBus: ...\n",
            "base/endpoints.py": "class ServiceEndpoints: ...\n",
            "app/x.py": "from base.db import connect\nx = connect()\n",
            "app/bootstrap.py": "from base.db import connect\nx = connect()\n",
            "app/env.py": "import os\nfrom base.config import settings\nx = os.environ['X']\n",
        },
    )


def ambient_rule_change(root: Path) -> None:
    rule_change(root)
    write(
        root,
        {
            "meizel.policy.toml": AMBIENT_POLICY,
            "base/meizel.toml": '[state]\n"config.py" = ["settings"]\n"db.py" = ["db"]\n'
            '"events.py" = ["bus"]\n"endpoints.py" = ["endpoints"]\n',
            "app/meizel.toml": '[roots]\n"bootstrap.py" = { kinds = ["db"], why = "opens the database" }\n'
            '"env.py" = { kinds = ["environ", "settings"], why = "the process environment boundary" }\n',
        },
    )


@pytest.mark.parametrize("added", [False, True])
def test_retired_schema_comparison_counts_old_reads_but_rejects_new_reads(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str], added: bool
) -> None:
    root = ambient_base(make_repo)
    ambient_rule_change(root)
    if added:
        write(root, {"app/x.py": "from base.db import connect\nx = connect()\ny = connect()\n"})
    record(root)
    capsys.readouterr()
    assert diff(root) == (1 if added else 0)
    out = capsys.readouterr().out
    row = next(line.split() for line in out.splitlines() if line.startswith("ambient-read"))
    assert row[1:] == ["0", "1", "2" if added else "1", "+1", "+1" if added else "+0"]
    if added:
        assert "2 recorded, 1 found" in out


DECLARED_BASE = {
    "meizel.toml": '[roles]\napplication = ["app"]\ntooling = ["rules"]\n',
    "app/__init__.py": "",
    "app/daemon.py": "import os\n\nvalue = os.environ['A']\n",
    "app/meizel.toml": '[debt.ambient-read]\n"daemon.py#environ os.environ" = 1\n',
    "rules/engine.py": "RULE = 1\n",
}


# meiva:note
# No line of code changes: the read leaves the record because the device stopped measuring it.
def test_a_root_declaration_is_a_rule_change_and_not_repayment(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(DECLARED_BASE)
    commit(root)
    rule_change(root)
    write(
        root,
        {
            "app/meizel.toml": '[roots]\n"daemon.py" = { kinds = ["environ"], why = "the process entry" }\n'
        },
    )
    assert run(root, "debt", "prune") == 0
    write(root, {})
    capsys.readouterr()
    assert plain_diff(root) == 0
    out = capsys.readouterr().out
    row = next(line.split() for line in out.splitlines() if line.startswith("ambient-read"))
    assert row[1:] == ["1", "0", "0", "-1", "+0"]
    assert "meizel: rules -1 / content +0" in out
    assert "what this change edits in the measurement" in out
    assert "app/meizel.toml [roots] daemon.py +" in out


# meiva:note
# The other direction: the device starts measuring the read again, and the code did not move.
def test_taking_a_root_away_is_a_rule_change_and_not_new_debt(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(
        {
            **DECLARED_BASE,
            "app/meizel.toml": '[roots]\n"daemon.py" = { kinds = ["environ"], why = "the process entry" }\n',
        }
    )
    commit(root)
    rule_change(root)
    write(root, {"app/meizel.toml": ""})
    assert run(root, "debt", "record", ".") == 0
    write(root, {})
    capsys.readouterr()
    assert diff(root) == 0
    out = capsys.readouterr().out
    row = next(line.split() for line in out.splitlines() if line.startswith("ambient-read"))
    assert row[1:] == ["0", "1", "1", "+1", "+0"]
    assert "meizel: rules +1 / content +0" in out
    assert "app/meizel.toml [roots] daemon.py -" in out


@pytest.mark.parametrize(
    "policy",
    [
        LEGACY_POLICY + "\n[unknown]\n",
        LEGACY_POLICY.replace('db-handles = ["base.db:Database"]', "db-handles = [3]"),
    ],
)
def test_retired_schema_never_bypasses_the_unreadable_policy_gate(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str], policy: str
) -> None:
    root = ambient_base(make_repo, policy)
    ambient_rule_change(root)
    assert diff(root) == 2
    assert "cannot read the manifests" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("source", "old_policy", "new_policy"),
    [
        (
            {"app/x.py": "import app\napp.after()\n"},
            '[purity]\nimpure-calls = ["app.before"]\n',
            '[purity]\nimpure-calls = ["app.after"]\n',
        ),
    ],
)
def test_rule_comparison_refreshes_policy_after_validating_the_old_snapshot(
    make_repo: RepoFactory,
    capsys: pytest.CaptureFixture[str],
    source: dict[str, str],
    old_policy: str,
    new_policy: str,
) -> None:
    root = base_repo(
        make_repo,
        {
            **source,
            "app/__init__.py": "def before(): ...\ndef after(): ...\n",
            "app/meizel.toml": 'purity = "strict-to-be"\n',
            "meizel.policy.toml": old_policy,
        },
    )
    rule_change(root)
    write(root, {"meizel.policy.toml": new_policy})
    record(root)
    capsys.readouterr()
    assert diff(root) == 0
    row = next(
        line.split()
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("impure-call")
    )
    assert row[1:] == ["0", "1", "1", "+1", "+0"]


def test_type_cast_rule_baseline_uses_existing_application_content(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = base_repo(
        make_repo,
        {"app/casts.py": "from typing import cast\ndef f(): return cast(int, value)\n"},
    )
    rule_change(root)
    record(root)
    capsys.readouterr()
    assert diff(root) == 0
    out = capsys.readouterr().out
    assert "type-cast" in out
    assert "no content growth" in out


def test_any_collaborator_rule_baseline_uses_existing_application_content(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = base_repo(
        make_repo,
        {"app/collab.py": "from typing import Any\ndef f(x: Any) -> int: return 1\n"},
    )
    rule_change(root)
    record(root)
    capsys.readouterr()
    assert diff(root) == 0
    out = capsys.readouterr().out
    assert "any-collaborator" in out
    assert "no content growth" in out


@pytest.mark.parametrize("new_path", ["app/casts.py", "app/new_casts.py"])
def test_type_cast_rule_cannot_trade_an_old_group_for_new_content(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str], new_path: str
) -> None:
    root = base_repo(
        make_repo,
        {"app/casts.py": "from typing import cast\ndef f(): return cast(int, value)\n"},
    )
    rule_change(root)
    record(root)
    write(
        root,
        {
            "app/casts.py": "",
            new_path: "from typing import cast\ndef g(): return cast(int, value)\n",
        },
    )
    assert run(root, "debt", "prune") == 0
    record(root)
    capsys.readouterr()
    assert diff(root) == 1
    out = capsys.readouterr()
    assert "cast g" in out.out
    assert "1 recorded, 0 found" in out.out
    assert "grew" in out.err


READER_BASE = {
    "meizel.toml": '[roles]\napplication = ["app", "lib"]\n\n[uses]\n"app" = ["lib"]\n',
    "lib/__init__.py": "",
    "lib/meizel.toml": 'exports = ["env.py"]\n\n[roots]\n"env.py" = { kinds = ["environ"], why = "returns the environment" }\n',
    "lib/env.py": "import os\n\n\ndef read():\n    return os.environ\n",
    "app/__init__.py": "",
    "app/use.py": "from lib.env import read\n\nx = read()\n",
}
READERS = '\n[ambient-readers.environ]\n"lib.env.read()" = "returns the environment"\n'


def reader_diff(root: Path) -> int:
    return run(
        root,
        "debt",
        "diff",
        "--against",
        "HEAD",
        "--on-growth",
        "fail",
        "--waive-if-changed",
        "meizel.policy.toml",
    )


def add_reader(root: Path) -> None:
    policy = (root / "meizel.policy.toml").read_text(encoding="utf-8")
    write(root, {"meizel.policy.toml": policy + READERS})


def test_listing_a_reader_in_the_policy_is_a_rule_change_not_content_growth(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(READER_BASE)
    commit(root)
    add_reader(root)
    assert run(root, "debt", "record", ".") == 0
    write(root, {})
    capsys.readouterr()
    assert reader_diff(root) == 0
    out = capsys.readouterr().out
    assert "ambient-read" in out
    assert "+1" in out
    assert "no content growth" in out


def test_a_policy_change_that_also_adds_a_call_site_fails(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(READER_BASE)
    commit(root)
    add_reader(root)
    write(root, {"app/other.py": "from lib.env import read\n\ny = read()\n"})
    assert run(root, "debt", "record", ".") == 0
    write(root, {})
    capsys.readouterr()
    assert reader_diff(root) == 1
    assert "other.py#environ lib.env.read" in capsys.readouterr().out


UNMASK_BASE = {
    "meizel.toml": '[roles]\napplication = ["app"]\ntooling = ["rules"]\n',
    "app/__init__.py": "",
    "app/daemon.py": "import os\n\nvalue = os.environ['A']\n",
    "app/meizel.toml": '[roots]\n"daemon.py" = { kinds = ["environ"], why = "the process entry" }\n',
    "rules/engine.py": "RULE = 1\n",
}


def test_removing_a_root_and_recording_the_reads_it_hid_is_not_content_growth(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(UNMASK_BASE)
    commit(root)
    rule_change(root)
    write(root, {"app/meizel.toml": ""})
    assert run(root, "debt", "record", ".") == 0
    write(root, {})
    assert "daemon.py#environ os.environ" in (root / "app" / "meizel.toml").read_text()
    capsys.readouterr()
    assert diff(root) == 0
    out = capsys.readouterr().out
    assert "no content growth" in out
    assert "+1" in out


def test_a_read_the_removed_root_never_covered_is_still_content_growth(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(UNMASK_BASE)
    commit(root)
    rule_change(root)
    write(
        root,
        {"app/meizel.toml": "", "app/daemon.py": "import time\n\nvalue = time.time()\n"},
    )
    assert run(root, "debt", "record", ".") == 0
    write(root, {})
    capsys.readouterr()
    assert diff(root) == 1
    assert "daemon.py#clock time.time" in capsys.readouterr().out


def test_the_read_of_another_file_of_the_directory_is_still_content_growth(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo({**UNMASK_BASE, "app/other.py": "VALUE = 1\n"})
    commit(root)
    rule_change(root)
    write(
        root,
        {"app/meizel.toml": "", "app/other.py": "import os\n\nVALUE = os.environ['B']\n"},
    )
    assert run(root, "debt", "record", ".") == 0
    write(root, {})
    capsys.readouterr()
    assert diff(root) == 1
    assert "other.py#environ os.environ" in capsys.readouterr().out


GUARD = "import lib\n\ndef guard(monkeypatch):\n    monkeypatch.setattr(lib, 'helper', lambda: 2)\n"
APPROVAL = '[approved-patches.files]\n"app/tests/guards.py" = "keeps the suite off the host"\n'
PATCHING = {
    "lib/__init__.py": "def helper() -> int:\n    return 1\n",
    "app/__init__.py": "",
    "app/tests/__init__.py": "",
    "app/tests/guards.py": GUARD,
    "rules/engine.py": "RULE = 1\n",
}


def patch_repo(make_repo: RepoFactory, policy: str) -> Path:
    root = make_repo({**PATCHING, "meizel.policy.toml": policy})
    run(root, "sync", "--declare", ".")
    write(root, {})
    run(root, "debt", "record", ".")
    write(root, {})
    commit(root)
    return root


# meiva:note
# An approval is part of the device: approving a file stops the measurement, and no line of its code
# changed, so the drop is the rules column.
def test_approving_a_file_is_a_rule_change_and_not_repayment(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = patch_repo(make_repo, "")
    rule_change(root)
    write(root, {"meizel.policy.toml": APPROVAL})
    assert run(root, "debt", "prune") == 0
    write(root, {})
    capsys.readouterr()
    assert diff(root) == 0
    out = capsys.readouterr().out
    [row] = [line.split() for line in out.splitlines() if line.startswith("first-party-patch")]
    assert row[1:] == ["1", "0", "0", "-1", "+0"]
    assert "+ [approved-patches.files] app/tests/guards.py" in out
    assert "no content growth" in out


def test_taking_an_approval_away_and_recording_the_patches_is_not_content_growth(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = patch_repo(make_repo, APPROVAL)
    rule_change(root)
    write(root, {"meizel.policy.toml": ""})
    assert run(root, "debt", "record", ".") == 0
    write(root, {})
    capsys.readouterr()
    assert diff(root) == 0
    assert "no content growth" in capsys.readouterr().out


def test_taking_an_approval_away_does_not_excuse_a_patch_added_elsewhere(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = patch_repo(make_repo, APPROVAL)
    rule_change(root)
    write(root, {"meizel.policy.toml": "", "app/tests/other.py": GUARD})
    assert run(root, "debt", "record", ".") == 0
    write(root, {})
    capsys.readouterr()
    assert diff(root) == 1
    out = capsys.readouterr().out
    assert "other.py#monkeypatch.setattr lib.helper" in out
    assert "guards.py#" not in out


# meiva:note
# The old patch is the rules' business; the one this change adds is the content's.
def test_a_patch_added_where_an_approval_was_taken_away_is_content_growth(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = patch_repo(make_repo, APPROVAL)
    rule_change(root)
    extra = "\n\ndef more(monkeypatch):\n    monkeypatch.setattr(lib, 'helper', lambda: 3)\n    exec('1')\n"
    write(root, {"meizel.policy.toml": "", "app/tests/guards.py": GUARD + extra})
    assert run(root, "debt", "record", ".") == 0
    write(root, {})
    capsys.readouterr()
    assert diff(root) == 1
    out = capsys.readouterr().out
    assert "guards.py#exec" in out
    assert "guards.py#monkeypatch.setattr lib.helper (first-party-patch) 2 recorded, 1 found" in out


# meiva:note
# The failure names the ref and the table shows the movement; the block after it says what a grown
# baseline costs and where to look. Asserted on fragments, so the wording stays free to change.
def test_the_growth_failure_says_why_and_where_to_look(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = base_repo(make_repo)
    rule_change(root)
    write(root, {"app/y.py": "import lib._hidden\n"})
    record(root)
    capsys.readouterr()
    assert diff(root) == 1
    err = capsys.readouterr().err.lower()

    assert "only shrink" in err
    assert "debt diff --by-target" in err
    assert "meizel/docs/checker/debt/debt.meiva.okf.md" in err
