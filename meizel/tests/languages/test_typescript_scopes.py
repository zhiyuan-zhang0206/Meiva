# meiva:note
# The `[roots]` warning for directories the selected TypeScript scopes do not judge.
#
# These contracts need the real Node producer: a repository that contains `.ts` files makes `check`
# run the locked TypeScript package, and without it the run refuses with a compiler error instead of
# reaching the warning. They therefore live in the native lane (`NATIVE_TEST_PATHS`), like the other
# compiler contracts, and the static lane excludes this file.
from __future__ import annotations

from pathlib import Path

import pytest

from meizel.tests.synthetic import RepoFactory, run, write

# meiva:note
# The measured hole (#5691, probe B): a `[roots]` table in a directory outside
# `[typescript].scopes` is parsed into the manifest and then consulted by nothing, because the
# exemption is read when that file's own reads are judged and the file is never scanned. Adding the
# declaration and deleting it again left `check` byte-identical.
SCOPED = "ui/web/src/lib"
OUT_OF_SCOPE = "ui/web/src/components"
POLICY_JUDGING_LIB = '[typescript]\nscopes = ["ui/web/src/lib"]\n'
READS_THE_CLOCK_TS = "export const stamp = (): number => Date.now();\n"
ROOT_TABLE = '[roots]\n"reading.ts" = { kinds = ["clock"], why = "stamps a page event" }\n'


def _scoped_repo(make_repo: RepoFactory, *, scopes: str, declare: bool) -> Path:
    manifest = 'exports = ["reading.ts"]\n' + (ROOT_TABLE if declare else "")
    return make_repo(
        {
            "ui/meizel.toml": "",
            "ui/web/meizel.toml": "",
            "ui/web/src/meizel.toml": "",
            f"{SCOPED}/reading.ts": READS_THE_CLOCK_TS,
            f"{SCOPED}/meizel.toml": 'exports = ["reading.ts"]\n',
            f"{OUT_OF_SCOPE}/reading.ts": READS_THE_CLOCK_TS,
            f"{OUT_OF_SCOPE}/deep/reading.ts": READS_THE_CLOCK_TS,
            f"{OUT_OF_SCOPE}/deep/meizel.toml": 'exports = ["reading.ts"]\n',
            f"{OUT_OF_SCOPE}/meizel.toml": manifest,
            "meizel.policy.toml": scopes,
        }
    )


def test_a_roots_table_outside_the_scopes_is_reported(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _scoped_repo(make_repo, scopes=POLICY_JUDGING_LIB, declare=True)
    assert run(root, "check") == 0
    err = capsys.readouterr().err
    assert f"meizel: warning: {OUT_OF_SCOPE}/meizel.toml:3:" in err
    assert "exempt nothing" in err
    assert "ui/web/src/lib" in err


def test_an_out_of_scope_manifest_without_roots_says_nothing(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _scoped_repo(make_repo, scopes=POLICY_JUDGING_LIB, declare=False)
    assert run(root, "check") == 0
    assert "warning" not in capsys.readouterr().err


def test_a_roots_table_inside_the_scopes_is_silent(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    scopes = f'[typescript]\nscopes = ["{SCOPED}", "{OUT_OF_SCOPE}"]\n'
    root = _scoped_repo(make_repo, scopes=scopes, declare=True)
    assert run(root, "check") == 0
    assert "warning" not in capsys.readouterr().err


def test_the_warning_follows_the_declaration(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _scoped_repo(make_repo, scopes=POLICY_JUDGING_LIB, declare=True)
    assert run(root, "check") == 0
    assert "warning" in capsys.readouterr().err
    write(root, {f"{OUT_OF_SCOPE}/meizel.toml": 'exports = ["reading.ts"]\n'})
    assert run(root, "check") == 0
    assert "warning" not in capsys.readouterr().err


def test_a_python_directory_is_judged_without_any_scope_declaration(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(
        {
            "app/pids.py": "import os\n\n\ndef pid() -> int:\n    return os.getpid()\n",
            "app/meizel.toml": 'exports = ["pids.py"]\n\n[roots]\n"pids.py" = { kinds = ["pid"], why = "the process identity" }\n',
            "meizel.policy.toml": POLICY_JUDGING_LIB,
        }
    )
    assert run(root, "check") == 0
    assert "warning" not in capsys.readouterr().err


def test_a_file_target_in_the_warned_directory_reports_it(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _scoped_repo(make_repo, scopes=POLICY_JUDGING_LIB, declare=True)
    assert run(root, "check", f"{OUT_OF_SCOPE}/reading.ts") == 0
    assert "exempt nothing" in capsys.readouterr().err
    assert run(root, "check", f"{SCOPED}/reading.ts") == 0
    assert "exempt nothing" not in capsys.readouterr().err


# meiva:note
# Only a *file* target stands in for the manifest that judges it. A directory target already says
# which directories were asked about, so a parent's inert table stays out of a run about a
# subdirectory -- the table names files directly in its own directory, not in that one.
def test_a_subdirectory_target_does_not_report_its_parents_table(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _scoped_repo(make_repo, scopes=POLICY_JUDGING_LIB, declare=True)
    assert run(root, "check", f"{OUT_OF_SCOPE}/deep") == 0
    assert "exempt nothing" not in capsys.readouterr().err
    assert run(root, "check", f"{OUT_OF_SCOPE}/deep/reading.ts") == 0
    assert "exempt nothing" not in capsys.readouterr().err
    assert run(root, "check", OUT_OF_SCOPE) == 0
    assert "exempt nothing" in capsys.readouterr().err


# meiva:note
# The remedy must not send an author down a route that cannot work. A `[roots]` key has to name a
# Python or TypeScript file, so the reachable hole with no way out is a directory whose only such
# file is a `.d.ts` declaration: the checker reads source, never a declaration, so a scope entry
# cannot help and deleting the entries is the only repair.
DECLARES_A_CONSTANT = "export declare const PORT: number;\n"
DECLARATION_ROOT_TABLE = (
    '[roots]\n"types.d.ts" = { kinds = ["clock"], why = "stamps a page event" }\n'
)


def test_a_declaration_only_directory_is_told_to_delete_the_entries(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(
        {
            f"{SCOPED}/reading.ts": READS_THE_CLOCK_TS,
            f"{SCOPED}/meizel.toml": 'exports = ["reading.ts"]\n',
            f"{OUT_OF_SCOPE}/types.d.ts": DECLARES_A_CONSTANT,
            f"{OUT_OF_SCOPE}/meizel.toml": DECLARATION_ROOT_TABLE,
            "meizel.policy.toml": POLICY_JUDGING_LIB,
        }
    )
    assert run(root, "check") == 0
    err = capsys.readouterr().err
    assert f"meizel: warning: {OUT_OF_SCOPE}/meizel.toml:2:" in err
    assert err.index("delete the entries") < err.index("to the scopes")
    assert "unless a `.d.ts` declaration is all it names" in err
