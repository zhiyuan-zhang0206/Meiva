# meiva:note
# A changed test file must be selected by the selector that reads the change.

from __future__ import annotations

from pathlib import Path

from scripts.ci import test_selector


# meiva:note
# #8578 shipped a first version whose own test never ran: the file was collectable, but the run
# selected for that pull request did not include it, so it entered main unexecuted. The rule this
# pins is narrow: a change to a test file must make that file run — either the whole suite or a
# selection that contains it.
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
