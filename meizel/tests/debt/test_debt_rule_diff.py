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
    assert "no content growth" in out


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
            "app/ambient_roots.toml": 'db = ["bootstrap.py"]\nsettings = ["env.py"]\n',
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
            "app/meizel.toml": '[roots]\n"bootstrap.py" = ["db"]\n'
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


def test_new_root_permission_is_not_copied_into_the_historical_baseline(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = ambient_base(make_repo)
    ambient_rule_change(root)
    path = root / "app/meizel.toml"
    write(root, {"app/meizel.toml": path.read_text() + '"x.py" = ["db"]\n'})
    record(root)
    capsys.readouterr()
    assert diff(root) == 0
    row = next(
        line.split()
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("ambient-read")
    )
    assert row[1:] == ["0", "1", "0", "+1", "-1"]


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


@pytest.mark.parametrize("registry", ["unknown = []\n", "db = [3]\n", "settings = []\n"])
def test_malformed_legacy_root_registry_still_stops_comparison(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str], registry: str
) -> None:
    root = ambient_base(make_repo)
    write(root, {"app/ambient_roots.toml": registry})
    commit(root)
    ambient_rule_change(root)
    assert diff(root) == 2
    error = capsys.readouterr().err
    assert "historical root" in error or "list of modules" in error


@pytest.mark.parametrize(
    ("source", "old_policy", "new_policy"),
    [
        (
            {"app/x.py": "import app\napp.after()\n"},
            '[purity]\nimpure-calls = ["app.before"]\n',
            '[purity]\nimpure-calls = ["app.after"]\n',
        ),
        (
            {"app/x.py": "", "app/view.ts": "export const value = afterCall();\n"},
            '[typescript]\nscopes = ["app"]\nimpure-calls = ["beforeCall"]\n',
            '[typescript]\nscopes = ["app"]\nimpure-calls = ["afterCall"]\n',
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
