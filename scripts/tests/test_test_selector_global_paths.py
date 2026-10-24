from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.ci import test_selector
from scripts.ci.test_selector import PathClass

_LINT = "tests/test_lint_scan.py"
_PYPROJECT = """[project]
name = "demo"
dependencies = ["alpha"]

[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.pyright]
include = ["base"]

[tool.ruff]
line-length = 100
"""


def _write(root: Path, name: str, text: str = "") -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# meiva:note
# Fixed Git commands against a checkout the test owns; no input reaches a shell.
def _git(root: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(root: Path) -> Path:
    _write(root, "pyproject.toml", _PYPROJECT)
    _write(root, _LINT, "def test_scan(): pass\n")
    _write(root, "tests/unit/test_a.py", "def test_a(): pass\n")
    _write(
        root,
        "tests/unit/test_reads_pyproject.py",
        "from pathlib import Path\n"
        "ROOT = Path(__file__).resolve().parents[2]\n"
        "def test_x():\n"
        '    assert (ROOT / "pyproject.toml").read_text()\n',
    )
    _write(
        root,
        "tests/unit/test_reads_deploy.py",
        "from pathlib import Path\n"
        "ROOT = Path(__file__).resolve().parents[2]\n"
        "def test_x():\n"
        '    assert (ROOT / "deploy/lgtm/README.md").read_text()\n',
    )
    _write(root, "deploy/lgtm/README.md", "notes\n")
    _write(root, "deploy/lgtm/config.yml", "key: value\n")
    _write(root, "commands/review.md", "prompt\n")
    _write(root, "tests/fixtures/README.md", "notes\n")
    _write(root, "db/schema.sql", "select 1;\n")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "base")
    return root


def _base(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD")


def test_prose_under_a_global_prefix_is_not_global(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    checkout = test_selector.load_checkout(root)

    for path in ("deploy/lgtm/README.md", "commands/review.md", "tests/fixtures/README.md"):
        assert test_selector.classify_path(path, checkout) is PathClass.TREE_SCAN_ONLY, path
    for path in ("deploy/lgtm/config.yml", "db/schema.sql"):
        assert test_selector.classify_path(path, checkout) is PathClass.GLOBAL, path


def test_prose_under_a_global_prefix_runs_its_readers_and_the_tree_scan_tests(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)

    result = test_selector.select_tests(["deploy/lgtm/README.md"], repo_root=root)

    assert (result.decision, result.reason) == ("SELECTED", "owner-tests")
    assert result.tests == (_LINT, "tests/unit/test_reads_deploy.py")
    assert test_selector.select_tests(["deploy/lgtm/config.yml"], repo_root=root).reason == (
        "global-path:deploy/lgtm/config.yml"
    )


@pytest.mark.parametrize(
    "replacement",
    [
        ('include = ["base"]', 'include = ["base", "cli"]'),
        ("line-length = 100", "line-length = 88"),
        ("line-length = 100", "line-length = 100  # a comment changes no table"),
    ],
)
def test_checker_only_pyproject_changes_run_the_files_readers(
    tmp_path: Path, replacement: tuple[str, str]
) -> None:
    root = _repo(tmp_path)
    base = _base(root)
    old, new = replacement
    _write(root, "pyproject.toml", _PYPROJECT.replace(old, new))

    result = test_selector.select_tests(["pyproject.toml"], repo_root=root, base_ref=base)

    assert (result.decision, result.reason) == ("SELECTED", "owner-tests")
    assert result.tests == (_LINT, "tests/unit/test_reads_pyproject.py")


@pytest.mark.parametrize(
    "replacement",
    [
        ('testpaths = ["tests"]', 'testpaths = ["tests", "base/**/tests"]'),
        ('dependencies = ["alpha"]', 'dependencies = ["alpha", "beta"]'),
        ("[tool.ruff]", '[tool.hatch.build]\nname = "x"\n\n[tool.ruff]'),
        ('name = "demo"', 'name = "renamed"'),
    ],
)
def test_pyproject_changes_that_can_reach_a_test_run_stay_global(
    tmp_path: Path, replacement: tuple[str, str]
) -> None:
    root = _repo(tmp_path)
    base = _base(root)
    old, new = replacement
    _write(root, "pyproject.toml", _PYPROJECT.replace(old, new))

    result = test_selector.select_tests(["pyproject.toml"], repo_root=root, base_ref=base)

    assert (result.decision, result.reason) == ("FULL", "global-path:pyproject.toml")


def test_a_checker_change_does_not_launder_a_global_table_change(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    base = _base(root)
    text = _PYPROJECT.replace("line-length = 100", "line-length = 88")
    _write(root, "pyproject.toml", text.replace('"alpha"', '"alpha", "beta"'))

    result = test_selector.select_tests(["pyproject.toml"], repo_root=root, base_ref=base)

    assert (result.decision, result.reason) == ("FULL", "global-path:pyproject.toml")


def test_pyproject_stays_global_without_base_facts(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "pyproject.toml", _PYPROJECT.replace("line-length = 100", "line-length = 88"))

    result = test_selector.select_tests(["pyproject.toml"], repo_root=root)

    assert (result.decision, result.reason) == ("FULL", "global-path:pyproject.toml")


def test_a_pyproject_new_in_this_change_stays_global(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _git(root, "rm", "-q", "--cached", "pyproject.toml")
    _git(root, "-c", "user.name=Test", "-c", "user.email=t@example.com", "commit", "-qm", "drop")
    base = _base(root)
    _write(root, "pyproject.toml", _PYPROJECT)

    result = test_selector.select_tests(["pyproject.toml"], repo_root=root, base_ref=base)

    assert (result.decision, result.reason) == ("FULL", "global-path:pyproject.toml")
