from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

from meizel.allowances import parse
from meizel.contracts.analysis import render_policy
from meizel.contracts.sync import with_table
from meizel.language import ALLOWED_STDLIB_MODULES, IMPURE_STDLIB_MODULES
from meizel.tests.synthetic import (
    RepoFactory,
    analysis_of,
    commit,
    diagnostics,
    git,
    run,
    seat,
    write,
)

LOADER = {
    "lib/__init__.py": "",
    "app/__init__.py": "",
    "app/loader.py": "import importlib\nimportlib.import_module('lib')\n",
    "app/runner.py": "exec('1')\n",
}
MANIFEST = '[loads]\n"loader.py" = ["lib"]\n\n[external]\n"runner.py" = "runs the user\'s code"\n'
ROOT = '[uses]\n"app" = ["lib"]\n'
POLICY = (
    '[external]\n"app/runner.py" = "runs the user\'s code"\n\n[loads]\n"app/loader.py" = ["lib"]\n'
)


def repo(make_repo: RepoFactory, policy: str | None, manifest: str = MANIFEST) -> Path:
    files = {**LOADER, "app/meizel.toml": manifest, "meizel.toml": ROOT}
    if policy is not None:
        files["meizel.policy.toml"] = policy
    return make_repo(files)


def unlisted(root: Path) -> list[str]:
    return sorted(d.message for d in diagnostics(root, "unlisted-allowance"))


def test_listed_sites_are_allowed(make_repo: RepoFactory) -> None:
    root = repo(make_repo, POLICY)
    assert unlisted(root) == []


def test_a_site_the_policy_does_not_list_is_an_error_whatever_the_manifest_says(
    make_repo: RepoFactory,
) -> None:
    root = repo(make_repo, '[external]\n"app/other.py" = "x"\n\n[loads]\n')
    messages = unlisted(root)
    assert messages == [
        "[external] app/runner.py is not listed in meizel.policy.toml",
        "[loads] app/loader.py -> lib is not listed in meizel.policy.toml",
    ]
    [found] = [d for d in diagnostics(root, "unlisted-allowance") if "external" in d.message]
    assert found.path == "app/meizel.toml"
    assert "a reason written in the manifest does not count" in found.render()


def test_a_missing_policy_allows_nothing(make_repo: RepoFactory) -> None:
    files = {**LOADER, "app/meizel.toml": MANIFEST, "meizel.toml": ROOT, "meizel.policy.toml": ""}
    root = make_repo(files)
    assert len(diagnostics(root, "unlisted-allowance")) == 2


def test_a_loads_target_outside_the_listed_ones_is_an_error(make_repo: RepoFactory) -> None:
    manifest = '[loads]\n"loader.py" = ["lib", "app"]\n'
    root = repo(make_repo, POLICY, manifest)
    assert unlisted(root) == ["[loads] app/loader.py -> app is not listed in meizel.policy.toml"]


def test_policy_entries_nothing_uses_do_no_harm(make_repo: RepoFactory) -> None:
    root = repo(make_repo, POLICY + '"app/gone.py" = ["lib"]\n')
    assert unlisted(root) == []


@pytest.mark.parametrize(
    "policy",
    [
        "this is not toml",
        "[mystery]\n",
        "external = 3\n",
        '[external]\n"a.py" = ""\n',
        '[external]\n"a.py" = 3\n',
        '[loads]\n"a.py" = "lib"\n',
        "[ambient-state]\nno-such-section = []\n",
        '[ambient-state.sink-facades]\n"a.py" = ""\n',
        "[ambient-state]\npure-callees = 3\n",
        '[ambient-state]\npure-callees = ["ok", 3]\n',
        '[type-checking]\nfiles = ["a.py"]\n',
        '[clock-lattice.family-modules]\n"a.py" = "a list is expected"\n',
        '[single-owner]\npostgres-dial = ["a.py"]\n',
        "[mystery.files]\n",
    ],
)
def test_a_malformed_policy_fails_at_its_own_path(make_repo: RepoFactory, policy: str) -> None:
    root = repo(make_repo, policy)
    found = diagnostics(root, "policy-syntax")
    assert found, policy
    assert {d.path for d in found} == {"meizel.policy.toml"}


LINTS = (
    '[ambient-state]\npure-callees = ["re.compile"]\n\n'
    '[ambient-state.sink-facades]\n"a.py" = "a write-only sink"\n\n'
    '[clock-lattice]\nfamily-modules = ["a.py"]\n\n'
    '[single-owner.postgres-dial]\n"b.py" = "cannot go through the owner"\n\n'
    '[type-checking.files]\n"c.py" = "a heavy import"\n'
)


def test_the_tables_of_the_lint_rules_are_part_of_the_policy(make_repo: RepoFactory) -> None:
    root = repo(make_repo, POLICY + "\n" + LINTS)
    assert diagnostics(root, "policy-syntax") == []
    assert unlisted(root) == []


def test_policy_diff_lists_the_lint_tables_too(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repo(make_repo, POLICY + "\n" + LINTS)
    commit(root)
    changed = (
        POLICY
        + '\n[ambient-state]\npure-callees = ["re.compile", "typing.TypeVar"]\n\n'
        + '[ambient-state.sink-facades]\n"a.py" = "another reason"\n\n'
        + '[single-owner.postgres-dial]\n"d.py" = "added"\n'
    )
    write(root, {"meizel.policy.toml": changed})
    assert run(root, "policy", "diff", "--against", "HEAD") == 0
    out = capsys.readouterr().out.splitlines()
    assert "+ [ambient-state.pure-callees] typing.TypeVar" in out
    assert "~ [ambient-state.sink-facades] a.py: a write-only sink -> another reason" in out
    assert "+ [single-owner.postgres-dial] d.py = added" in out
    assert "- [single-owner.postgres-dial] b.py = cannot go through the owner" in out
    assert "- [clock-lattice.family-modules] a.py" in out
    assert "- [type-checking.files] c.py = a heavy import" in out


def test_default_arguments_have_a_table_and_it_may_be_empty(make_repo: RepoFactory) -> None:
    root = repo(make_repo, POLICY + "\n[default-arguments]\n")
    assert diagnostics(root, "policy-syntax") == []


def test_policy_diff_lists_added_removed_and_changed_entries(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repo(make_repo, POLICY)
    commit(root)
    changed = '[external]\n"app/runner.py" = "a new reason"\n"app/extra.py" = "added"\n\n[loads]\n'
    write(root, {"meizel.policy.toml": changed})
    assert run(root, "policy", "diff", "--against", "HEAD") == 0
    out = capsys.readouterr().out.splitlines()
    assert "+ [external] app/extra.py = added" in out
    assert "~ [external] app/runner.py: runs the user's code -> a new reason" in out
    assert "- [loads] app/loader.py = [lib]" in out


def test_policy_diff_is_silent_when_nothing_changed(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repo(make_repo, POLICY)
    commit(root)
    assert run(root, "policy", "diff", "--against", "HEAD") == 0
    assert capsys.readouterr().out == ""


def test_policy_diff_treats_an_unborn_policy_as_empty(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo({**LOADER, "app/meizel.toml": MANIFEST, "meizel.toml": ROOT})
    git(root, "rm", "-q", "--cached", "meizel.policy.toml")
    commit(root, "no policy")
    write(root, {"meizel.policy.toml": POLICY})
    assert run(root, "policy", "diff", "--against", "HEAD") == 0
    assert "+ [external] app/runner.py" in capsys.readouterr().out
    assert analysis_of(root).verdict is not None


def test_historical_policy_diff_keeps_retired_entries_and_surviving_reasons(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    from meizel.contracts.sync import prepare_policy_snapshot

    old = (
        '[ambient-state]\ndb-modules = ["base#db"]\ndb-handles = ["base:Database"]\n'
        '[ambient-state.sites]\n"a.py::cache" = "keep # and this reason"\n'
    )
    root = make_repo({"meizel.policy.toml": old})
    commit(root)
    metadata = prepare_policy_snapshot(seat(root).repo)
    assert metadata.named[("ambient-state", "db-handles")] == ("base:Database",)
    normalized = (root / "meizel.policy.toml").read_text()
    assert '"a.py::cache" = "keep # and this reason"' in normalized
    assert "db-modules" not in normalized and "db-handles" not in normalized
    assert run(root, "policy", "diff", "--against", "HEAD") == 0
    out = capsys.readouterr().out
    assert "- [ambient-state.db-modules] base#db" in out
    assert "- [ambient-state.db-handles] base:Database" in out
    assert "ambient-state.sites" not in out


TABLE = "approved-patches.files"


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("", "[external]\n"),
        ("[external]\n", "[external]\n"),
        ('[external]\n\n[approved-patches.files]\n"old.py" = "x"\n', "[external]\n"),
        ('[approved-patches.files]\n"old.py" = "x"\n\n[external]\n', "[external]\n"),
        (
            '[external]\n# note\n\n[approved-patches.files]\n"old.py" = "x"\n\n[loads]\n',
            "[external]\n# note\n\n[loads]\n",
        ),
    ],
)
def test_with_table_replaces_the_table_wherever_it_stands(text: str, kept: str) -> None:
    entries = {"b.py": 'quote " and backslash \\ and \u00e9 and \U0001f600', "a.py": "first"}
    written = with_table(text or "[external]\n", TABLE, entries)
    document = tomllib.loads(written)
    assert document["approved-patches"]["files"] == entries
    assert written.index('"a.py"') < written.index('"b.py"')
    assert tomllib.loads(with_table(written, TABLE, entries)) == document
    assert written.count("[approved-patches.files]") == 1
    without = with_table(text or "[external]\n", TABLE, {})
    assert without == kept
    assert "approved-patches" not in tomllib.loads(without)


def test_with_table_keeps_every_other_table_untouched() -> None:
    text = '[external]\n"a.py" = "x"\n\n[approved-patches.files]\n"old.py" = "y"\n\n[loads]\n"b.py" = ["lib"]\n'
    document = tomllib.loads(with_table(text, TABLE, {"new.py": "z"}))
    assert document["external"] == {"a.py": "x"}
    assert document["loads"] == {"b.py": ["lib"]}
    assert document["approved-patches"]["files"] == {"new.py": "z"}


def test_a_policy_line_is_found_inside_its_table_only() -> None:
    text = (
        '[external]\n"child.py" = "runs it"\n\n'
        '[approved-patches.files]\n"child.py" = "keeps the suite off the host"\n'
    )
    policy, problems = parse(text)
    assert problems == []
    assert policy.line_of("child.py") == 2
    assert policy.line_of("child.py", TABLE) == 5
    assert policy.line_of("absent.py", TABLE) == 1


def test_policy_diff_lists_a_clause_change_on_its_own(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    listed = (
        '[ambient-readers.pid]\n"app.reader()" = { why = "reads the process", unless = "euid" }\n'
    )
    root = repo(make_repo, POLICY + "\n" + listed)
    commit(root)
    changed = listed.replace('"euid"', '"uid"')
    write(root, {"meizel.policy.toml": POLICY + "\n" + changed})
    assert run(root, "policy", "diff", "--against", "HEAD") == 0
    out = capsys.readouterr().out.splitlines()
    assert "~ [ambient-readers.pid unless] app.reader(): unless euid -> unless uid" in out
    assert not [line for line in out if line.startswith(("+", "-"))]


READER = {"app/reader.py": 'import os\n\n\ndef host() -> str:\n    return os.environ["HOME"]\n'}
ROOTS = (
    '[roots]\n"reader.py" = { kinds = ["environ"], why = "the host environment is the input" }\n'
)


def surface_repo(make_repo: RepoFactory, manifest: str = MANIFEST) -> Path:
    files = {**LOADER, **READER, "app/meizel.toml": manifest, "meizel.toml": ROOT}
    return make_repo({**files, "meizel.policy.toml": POLICY})


def test_policy_surface_reports_an_added_root_as_narrower(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = surface_repo(make_repo)
    commit(root)
    write(root, {"app/meizel.toml": MANIFEST + "\n" + ROOTS})
    assert run(root, "policy", "surface", "--against", "HEAD") == 0
    out = capsys.readouterr().out
    assert "1 narrower" in out
    assert "**added** `app/meizel.toml [roots]` `reader.py` = [environ]" in out
    assert "| `app/meizel.toml` | `roots` | 1 | 0 | 0 | 0 |" in out


def test_policy_surface_reports_a_removed_allowance_as_wider(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = surface_repo(make_repo)
    commit(root)
    write(root, {"meizel.policy.toml": '[loads]\n"app/loader.py" = ["lib"]\n'})
    assert run(root, "policy", "surface", "--against", "HEAD") == 0
    out = capsys.readouterr().out
    assert "1 wider" in out
    assert "**removed** `meizel.policy.toml [external]` `app/runner.py`" in out


def test_policy_surface_does_not_call_a_reason_edit_a_direction(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = surface_repo(make_repo)
    commit(root)
    write(
        root,
        {"meizel.policy.toml": POLICY.replace("runs the user's code", "runs the schedule script")},
    )
    assert run(root, "policy", "surface", "--against", "HEAD") == 0
    out = capsys.readouterr().out
    assert "1 unknown" in out
    assert "**modified** `meizel.policy.toml [external]` `app/runner.py`" in out
    assert "narrower" not in out


def test_policy_surface_reads_a_vocabulary_the_other_way_round(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = surface_repo(make_repo)
    commit(root)
    write(
        root,
        {"meizel.policy.toml": POLICY + '\n[ambient-readers.environ]\n"os.environ" = "reads it"\n'},
    )
    assert run(root, "policy", "surface", "--against", "HEAD") == 0
    out = capsys.readouterr().out
    assert "1 wider" in out
    assert "**added** `meizel.policy.toml [ambient-readers.environ]` `os.environ`" in out


def test_policy_surface_pairs_a_declaration_with_git_rename(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = surface_repo(make_repo)
    commit(root)
    git(root, "mv", "app/runner.py", "app/launcher.py")
    write(
        root,
        {
            "meizel.policy.toml": POLICY.replace("app/runner.py", "app/launcher.py"),
            "app/meizel.toml": MANIFEST.replace('"runner.py"', '"launcher.py"'),
        },
    )
    assert run(root, "policy", "surface", "--against", "HEAD") == 0
    out = capsys.readouterr().out
    assert "1 moved" in out
    assert "**moved** `meizel.policy.toml [external]` `app/launcher.py` was `app/runner.py`" in out
    assert "**added**" not in out and "**removed**" not in out


def test_policy_surface_is_quiet_when_nothing_changed(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = surface_repo(make_repo)
    commit(root)
    assert run(root, "policy", "surface", "--against", "HEAD") == 0
    assert "no change" in capsys.readouterr().out


def test_policy_surface_claims_nothing_about_a_manifest_it_cannot_read(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = surface_repo(make_repo, manifest="[roots\n")
    commit(root)
    write(root, {"app/meizel.toml": MANIFEST + "\n" + ROOTS})
    assert run(root, "policy", "surface", "--against", "HEAD") == 0
    out = capsys.readouterr().out
    assert "1 unread" in out
    assert "**Could not be read**" in out
    assert "`app/meizel.toml`" in out
    assert "**added**" not in out and "no change" not in out


# meiva:note
# The third way to narrow, and the quietest: an `unless` clause stops counting the calls whose
# argument matches. It has no gate of its own, so this report is the only place it shows up.
def test_policy_surface_reports_an_added_unless_clause_as_narrower(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = surface_repo(make_repo)
    commit(root)
    narrower = (
        POLICY + '\n[ambient-readers.pid]\n"app.reader()" = { why = "reads the process", '
        'unless = "euid" }\n'
    )
    write(root, {"meizel.policy.toml": narrower})
    assert run(root, "policy", "surface", "--against", "HEAD") == 0
    out = capsys.readouterr().out
    assert "1 narrower" in out
    assert "**added** `meizel.policy.toml [ambient-readers.pid unless]` `app.reader()`" in out


def test_stdlib_module_classification_is_complete_and_disjoint() -> None:
    all_known = frozenset(sys.stdlib_module_names | {"__future__", "builtins"})
    assert all_known == ALLOWED_STDLIB_MODULES | IMPURE_STDLIB_MODULES
    assert frozenset() == ALLOWED_STDLIB_MODULES & IMPURE_STDLIB_MODULES
    assert "subprocess" in IMPURE_STDLIB_MODULES
    assert "socket" in IMPURE_STDLIB_MODULES
    assert "asyncio" in ALLOWED_STDLIB_MODULES
    assert "threading" in ALLOWED_STDLIB_MODULES
    assert "json" in ALLOWED_STDLIB_MODULES


def test_third_party_imports_fail_closed_unless_in_pure_third_party(
    make_repo: RepoFactory,
) -> None:
    root = make_repo(
        {
            "meizel.policy.toml": render_policy({}, {})
            + '\n[purity]\npure-third-party = ["pydantic"]\n',
            "pkg/__init__.py": "",
            "pkg/allowed.py": "import pydantic\n",
            "pkg/blocked.py": "import some_unknown_third_party\n",
            "pkg/meizel.toml": 'purity = "strict"\n',
        }
    )
    diags = diagnostics(root, "impure-call")
    assert len(diags) == 1
    assert "impure import `some_unknown_third_party` in strict module" in diags[0].message
    assert diags[0].path == "pkg/blocked.py"
