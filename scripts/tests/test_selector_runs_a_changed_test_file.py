# meiva:note
# A changed test file must be selected by the selector that reads the change.

from __future__ import annotations

from pathlib import Path

from scripts.ci import test_selector


# meiva:note
# What this pins is narrow and stands on its own: a change to a test file must make that file run —
# the whole suite, or a selection that contains it.
#
# The incident that prompted it was read wrong at first, and the correction is the useful part.
# #8578's first version had its own test selected, its lane included it, and pytest had started;
# the job was then superseded by the next push and cancelled, so the run left no verdict at all.
# A cancelled job and a silently dropped file look identical from outside — no JUnit, truncated
# log, the file still listed in the selection — so read `gh run view --json jobs` for `cancelled`
# before concluding a selection filtered it out. Selection reads the pull request's cumulative
# diff (`git merge-base BASE HEAD...HEAD`), so a superseded run re-selects the same files rather
# than falling back to the last push's increment.
def _repo(root: Path) -> Path:
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests", "base/**/tests"]\n', encoding="utf-8"
    )
    return root


def test_changing_a_test_file_selects_that_file(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    changed = "base/log/tests/test_secret_prefix.py"
    target = root / changed
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("def test_ok() -> None:\n    assert True\n", encoding="utf-8")

    result = test_selector.select_tests([changed], repo_root=root)

    assert result.decision == "FULL" or changed in result.tests, result.as_json()
