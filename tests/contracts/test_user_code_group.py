# meiva:note
# The `user-code` group is what agent code may import; the application never does.
#
# `execute_code` code runs in this environment, so the libraries an agent reaches for
# (pandas, scipy, matplotlib ...) are installed by default. None of them belongs to the
# application's own import graph, so they live in a `user-code` dependency group instead
# of `[project.dependencies]`. `[tool.uv] default-groups` makes every `uv sync` - a
# developer worktree, CI, a fleet update - install the same set as before, while a
# deployment that runs no agent code leaves the group out with `--no-group user-code`.
#
# The group is a promise, and these tests keep it checkable: every member declares the
# module name agent code imports, no shipped or test module imports one, no member is a
# runtime dependency, and the group stays a default group.
# meiva:link docs/conventions/dev-setup.md
from __future__ import annotations

import ast
import os
import re
import tomllib
from functools import cache
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SKIP_DIRS = frozenset(
    {".git", ".venv", ".worktrees", "node_modules", "__pycache__", ".cache", ".trunk"}
)

# meiva:note
# Distribution -> the module the group exists for. `execute_code` code imports the
# module, so this is the name an import scan has to match.
_USER_CODE_MODULES = {
    "pandas": "pandas",
    "scipy": "scipy",
    "scikit-learn": "sklearn",
    "statsmodels": "statsmodels",
    "matplotlib": "matplotlib",
    "openpyxl": "openpyxl",
    "xlsxwriter": "xlsxwriter",
    "python-docx": "docx",
    "pypdf": "pypdf",
    "lxml": "lxml",
    "python-dateutil": "dateutil",
    "aiohttp": "aiohttp",
    "rich": "rich",
    "tqdm": "tqdm",
    "tabulate": "tabulate",
    "watchdog": "watchdog",
}


def _toml(relative: str) -> dict[str, Any]:
    return tomllib.loads((_REPO_ROOT / relative).read_text(encoding="utf-8"))


def _distribution_name(requirement: str) -> str:
    # meiva:note
    # `psycopg[binary]>=3.2` -> `psycopg`: the name a group entry is compared by.
    return re.split(r"[<>=!~;\[]", requirement, maxsplit=1)[0].strip().lower()


def _group() -> dict[str, str]:
    groups = _toml("pyproject.toml")["dependency-groups"]
    return {_distribution_name(item): item for item in groups["user-code"]}


def test_default_groups_install_the_group_beside_dev() -> None:
    # meiva:note
    # This is the change's one new risk: drop `user-code` from the default set and every
    # `uv sync` - including the fleet update's `uv sync --frozen` - silently uninstalls the
    # toolbox from running agents' `execute_code` environment.
    defaults = _toml("pyproject.toml")["tool"]["uv"]["default-groups"]

    assert "user-code" in defaults, (
        "a `uv sync` installs the default groups only: without `user-code` here, the "
        "next sync removes the agent toolbox from every existing environment"
    )
    assert "dev" in defaults, "the default set is 'dev' plus this group, never this group alone"


def test_every_group_member_declares_its_import_name() -> None:
    declared = set(_group())

    assert declared == set(_USER_CODE_MODULES), (
        "add or remove the member here with the module name agent code imports; "
        f"missing={sorted(declared - set(_USER_CODE_MODULES))} "
        f"stale={sorted(set(_USER_CODE_MODULES) - declared)}"
    )


def test_the_group_is_not_a_runtime_dependency() -> None:
    runtime = {
        _distribution_name(item) for item in _toml("pyproject.toml")["project"]["dependencies"]
    }

    assert not (set(_group()) & runtime), (
        "a library the application itself needs stays in [project.dependencies]; the "
        "group only holds what no first-party module imports"
    )


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _python_files() -> list[Path]:
    # meiva:note
    # `rglob` would descend into each sibling worktree under `.worktrees/`.
    paths: list[Path] = []
    for directory, dirnames, filenames in os.walk(_REPO_ROOT):
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS]
        paths.extend(Path(directory) / name for name in filenames if name.endswith(".py"))
    return paths


def _absolute_imports(tree: ast.AST) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append((node.module, node.lineno))
    return found


def _group_imports(path: Path, wanted: tuple[str, ...]) -> list[tuple[str, int]]:
    # meiva:note
    # The text probe keeps ~5k files out of the parser; the AST then decides, so a comment
    # or a string that names the module is not a violation.
    source = _read(path)
    if source is None or not any(module in source for module in wanted):
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    return [
        (want, line)
        for module, line in _absolute_imports(tree)
        for want in wanted
        if module == want or module.startswith(want + ".")
    ]


@cache
def _import_sites() -> dict[str, list[str]]:
    wanted = tuple(sorted(set(_USER_CODE_MODULES.values())))
    sites: dict[str, list[str]] = {module: [] for module in wanted}
    for path in _python_files():
        for module, line in _group_imports(path, wanted):
            sites[module].append(f"{path.relative_to(_REPO_ROOT)}:{line}")
    return sites


@pytest.mark.parametrize(("distribution", "module"), sorted(_USER_CODE_MODULES.items()))
def test_no_module_imports_a_group_member(distribution: str, module: str) -> None:
    importers = _import_sites()[module]

    assert not importers, (
        f"{distribution} is imported by first-party code, so it is a runtime dependency "
        f"rather than agent convenience; move it to [project.dependencies]: {importers}"
    )
