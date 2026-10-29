from __future__ import annotations

from pathlib import Path

import pytest

from meizel import lint_common
from meizel.roles import pytest_test_hosts

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_every_framework_dir_exists() -> None:
    roots = lint_common.scan_roots(_REPO_ROOT, lint_common.FRAMEWORK_DIRS)
    assert [r.name for r in roots] == list(lint_common.FRAMEWORK_DIRS)


def test_missing_scan_dir_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "agent").mkdir()
    with pytest.raises(FileNotFoundError, match="plugins, mcps"):
        lint_common.scan_roots(tmp_path, ("agent", "plugins", "mcps"))


@pytest.mark.parametrize(
    "pattern", ["/**/tests", "*/**/tests", "base/**/test", "base/../tests", "base\\child/**/tests"]
)
def test_pytest_test_scope_rejects_unsupported_directory_patterns(pattern: str) -> None:
    with pytest.raises(ValueError, match="unsupported pytest testpaths"):
        pytest_test_hosts(f"[tool.pytest.ini_options]\ntestpaths = [{pattern!r}]\n")


def test_pytest_test_scope_rejects_duplicate_hosts() -> None:
    with pytest.raises(ValueError, match="duplicate pytest test host"):
        pytest_test_hosts('[tool.pytest.ini_options]\ntestpaths = ["tests", "tests"]\n')


def test_git_owns_directory_source_admission(tmp_path: Path) -> None:
    import subprocess

    def git(*args: str) -> None:
        # meiva:note
        # Fixed Git argv in an isolated fixture, without invoking a shell.
        subprocess.run(  # noqa: S603
            ["git", "-C", str(tmp_path), *args], check=True, capture_output=True
        )

    git("init", "--quiet")
    (tmp_path / ".gitignore").write_text(".venv/\napp/vendor/\n", encoding="utf-8")
    for relative in ("app/old.py", "app/.venv/bad.py", "app/vendor/forced.py"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n", encoding="utf-8")
    git("add", ".gitignore", "app/old.py")
    git("add", "--force", "app/vendor/forced.py")
    (tmp_path / "app/new.py").write_text("x = 2\n", encoding="utf-8")
    (tmp_path / "app/link.py").symlink_to(tmp_path / "app/.venv/bad.py")
    expected = ["app/new.py", "app/old.py", "app/vendor/forced.py"]
    assert lint_common.resolve_targets([str(tmp_path / "app")], tmp_path) == (expected, [])
    assert lint_common.python_files([tmp_path / "app"], tmp_path) == [
        tmp_path / path for path in expected
    ]
    # meiva:note
    # An explicit leaf remains an intentional audit, unlike a directory scan.
    assert lint_common.python_files([tmp_path / "app/.venv/bad.py"], tmp_path) == [
        tmp_path / "app/.venv/bad.py"
    ]


def test_directory_scan_does_not_follow_a_tracked_parent_replaced_by_a_link(tmp_path: Path) -> None:
    import subprocess

    root = tmp_path / "repo"
    source = root / "app/owned/source.py"
    source.parent.mkdir(parents=True)
    source.write_text("value = 1\n")
    # meiva:note
    # Fixed Git argv build only this fixture's index; later links do not change its ownership.
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True, capture_output=True)  # noqa: S603
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)  # noqa: S603
    source.parent.rename(tmp_path / "foreign")
    source.parent.symlink_to(tmp_path / "foreign", target_is_directory=True)
    assert lint_common.python_files([root / "app"], root) == []


# meiva:note
# Admission reuse is scoped to one lint run, and the scope is what makes it safe:
# inside the run the checkout is read once (a lint asks per target and one run asked
# 21 times), and outside it every read goes back to git, so a file created between
# two runs is never judged by the previous run's file set. Both halves are pinned
# here because the fast path and the correct path are the same code.
# meiva:link meizel/docs/lint/lint-common-admission.meiva.okf.md
def _git_fixture(root: Path, *files: str) -> None:
    import subprocess

    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True, capture_output=True)  # noqa: S603
    for relative in files:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)  # noqa: S603


def test_admission_reads_the_checkout_once_per_run(tmp_path: Path) -> None:
    _git_fixture(tmp_path / "repo", "app/one.py", "app/two.py")
    root = tmp_path / "repo"
    with lint_common.admission():
        first = lint_common.python_files([root / "app"], root)
        second = lint_common.python_files([root / "app/two.py"], root)
        stats = lint_common.admission.stats()
    assert first == [root / "app/one.py", root / "app/two.py"]
    assert second == [root / "app/two.py"]
    assert stats["reads"] == 1, stats
    assert stats["reuses"] >= 1, stats


def test_admission_scope_ends_with_the_run(tmp_path: Path) -> None:
    _git_fixture(tmp_path / "repo", "app/one.py")
    root = tmp_path / "repo"
    with lint_common.admission():
        assert lint_common.python_files([root / "app"], root) == [root / "app/one.py"]
    (root / "app/created_after_the_run.py").write_text("value = 2\n", encoding="utf-8")
    assert lint_common.python_files([root / "app"], root) == [
        root / "app/created_after_the_run.py",
        root / "app/one.py",
    ]


def test_an_unrelated_touch_keeps_the_admitted_set(tmp_path: Path) -> None:
    _git_fixture(tmp_path / "repo", "app/one.py", "docs/notes.md")
    root = tmp_path / "repo"
    before = lint_common.python_files([root / "app"], root)
    (root / "docs/notes.md").touch()
    assert lint_common.python_files([root / "app"], root) == before
