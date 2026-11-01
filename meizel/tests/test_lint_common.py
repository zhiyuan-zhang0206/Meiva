from __future__ import annotations

import errno
import os
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
    with lint_common.admission:
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
    with lint_common.admission:
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


# meiva:note
# The file set a lint may read is the checkout's: a path reached through a link is not in
# it, because git tracks a path, not how it is reached. `tracked_files(regular=True)`
# decides that, so the rule is pinned where the lints consume it — a directory's answer is
# remembered for one listing rather than rebuilt per component, and never across two reads.
def _regular(root: Path) -> set[str]:
    return set(lint_common.tracked_files(root, include_untracked=True, regular=True))


def test_a_file_that_is_a_link_is_not_regular(tmp_path: Path) -> None:
    _git_fixture(tmp_path / "repo", "pkg/real.py")
    root = tmp_path / "repo"
    (root / "pkg/link.py").symlink_to(root / "pkg/real.py")
    assert "pkg/real.py" in _regular(root)
    assert "pkg/link.py" not in _regular(root)


def test_a_file_below_a_linked_directory_is_not_regular(tmp_path: Path) -> None:
    _git_fixture(tmp_path / "repo", "app/owned/source.py")
    root = tmp_path / "repo"
    (root / "app/owned").rename(tmp_path / "foreign")
    (root / "app/owned").symlink_to(tmp_path / "foreign", target_is_directory=True)
    assert "app/owned/source.py" not in _regular(root)


def test_a_link_two_levels_above_the_file_is_found(tmp_path: Path) -> None:
    _git_fixture(tmp_path / "repo", "app/owned/deep/source.py")
    root = tmp_path / "repo"
    (root / "app/owned").rename(tmp_path / "foreign")
    (root / "app/owned").symlink_to(tmp_path / "foreign", target_is_directory=True)
    assert "app/owned/deep/source.py" not in _regular(root)


def test_a_rejected_link_does_not_hide_a_regular_sibling(tmp_path: Path) -> None:
    _git_fixture(tmp_path / "repo", "pkg/a.py", "pkg/b.py")
    root = tmp_path / "repo"
    (root / "pkg/link.py").symlink_to(root / "pkg/a.py")
    assert {"pkg/a.py", "pkg/b.py"} <= _regular(root)


def test_a_directory_replaced_by_a_link_is_read_again(tmp_path: Path) -> None:
    _git_fixture(tmp_path / "repo", "app/owned/source.py")
    root = tmp_path / "repo"
    assert "app/owned/source.py" in _regular(root)
    (root / "app/owned").rename(tmp_path / "foreign")
    (root / "app/owned").symlink_to(tmp_path / "foreign", target_is_directory=True)
    assert "app/owned/source.py" not in _regular(root)


def test_a_directory_replaced_by_a_regular_file_is_skipped(tmp_path: Path) -> None:
    _git_fixture(tmp_path / "repo", "app/keep.py", "app/owned/source.py")
    root = tmp_path / "repo"
    (root / "app/owned").rename(tmp_path / "moved")
    (root / "app/owned").write_text("not a directory\n", encoding="utf-8")
    kept = _regular(root)
    assert "app/keep.py" in kept
    assert "app/owned/source.py" not in kept


# meiva:note
# The other half of the error contract: an answer that is not "missing" still fails the
# caller, so no lint silently judges a smaller file set. A locked directory produces it on
# a normal runner but not under root, so the test passes the reader whose answer it needs
# instead of spending a skip site on the uid.
def test_a_file_the_filesystem_refuses_to_read_fails_the_caller(tmp_path: Path) -> None:
    _git_fixture(tmp_path / "repo", "app/owned/source.py")
    root = tmp_path / "repo"

    def unreadable(path: str) -> os.stat_result:
        raise PermissionError(errno.EACCES, "permission denied", path)

    with pytest.raises(PermissionError):
        lint_common.tracked_files(root, regular=True, lstat=unreadable)


def test_a_file_deleted_after_the_listing_is_skipped(tmp_path: Path) -> None:
    _git_fixture(tmp_path / "repo", "app/keep.py", "app/gone.py")
    root = tmp_path / "repo"
    (root / "app/gone.py").unlink()
    assert _regular(root) == {"app/keep.py"}


# meiva:note
# #8085 moved ui/web to pnpm, so a worktree created outside `scripts/setup-worktree.sh` has no
# `node_modules` and the analyzer dies with a module-not-found stack that reads like a regression
# in the change under test. The failure names the fix instead; any other analyzer failure keeps
# its own message.
def test_a_missing_analyzer_dependency_names_the_fix(tmp_path: Path) -> None:
    producer = tmp_path / "producer.mjs"
    producer.write_text("import 'no-such-analyzer-dependency';\n", encoding="utf-8")

    with pytest.raises(lint_common.MeizelError) as caught:
        lint_common.run_node_analyzer(producer, "{}")

    assert "scripts/setup-worktree.sh" in str(caught.value)


def test_an_analyzer_failure_keeps_its_own_message(tmp_path: Path) -> None:
    producer = tmp_path / "producer.mjs"
    producer.write_text("throw new Error('boom');\n", encoding="utf-8")

    with pytest.raises(lint_common.MeizelError) as caught:
        lint_common.run_node_analyzer(producer, "{}")

    assert "scripts/setup-worktree.sh" not in str(caught.value)
    assert "boom" in str(caught.value)


def test_meizel_source_code_contains_no_meiva_specific_hardcoded_literals() -> None:
    meizel_dir = Path(__file__).resolve().parents[1]
    forbidden = ["db_conn", "aops_pool", "meiva_builtins", "ui/web"]
    violations: list[str] = []
    for path in meizel_dir.rglob("*"):
        if not path.is_file():
            continue
        if "tests" in path.parts or "test_graph" in path.parts or "__pycache__" in path.parts:
            continue
        if path.suffix not in {".py", ".mjs", ".js"}:
            continue
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(meizel_dir)
        for term in forbidden:
            if term in text:
                violations.append(f"{rel}: contains forbidden literal {term!r}")
    assert violations == []
