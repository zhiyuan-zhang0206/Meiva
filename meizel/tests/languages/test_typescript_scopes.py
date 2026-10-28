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
