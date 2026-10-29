# meiva:note
# The top-level packages of the repository and the role of each, declared once.
#
# The root `meizel.toml` holds a `[roles]` table that maps a role to the top-level
# directories playing it. This module reads that table; every other place that needs
# to know which top-level packages exist (a scan scope, a test host list, a packaging
# list) calls `packages()` instead of keeping a copy, or is generated from the table by
# `python -m meizel roles write` and kept fresh by `python -m meizel check`.

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import TypeGuard

from meizel.checkout import GitRepo
from meizel.graph.model import MeizelError
from meizel.graph.universe import MANIFEST

__all__ = [
    "ROLE_NAMES",
    "all_packages",
    "check_table",
    "packages",
    "pytest_test_hosts",
    "roles",
    "select_packages",
]

# The closed set of roles; an unknown role is an error, never a new category.
ROLE_NAMES = ("application", "standalone", "templates", "examples", "tooling", "tests", "frontend")
_ROOT = Path(__file__).resolve().parents[1]


def _is_raw_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _is_table(value: object) -> TypeGuard[dict[str, object]]:
    return _is_raw_dict(value) and all(isinstance(k, str) for k in value)


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_str_list(value: object) -> TypeGuard[list[str]]:
    return _is_object_list(value) and all(isinstance(item, str) for item in value)


def check_table(table: object) -> tuple[dict[str, tuple[str, ...]], list[tuple[str, str]]]:
    """The roles of a parsed `[roles]` table and the `(key, message)` problems found in it."""
    if not _is_table(table):
        return {}, [("roles", "[roles] must be a table")]
    found: dict[str, tuple[str, ...]] = {}
    problems: list[tuple[str, str]] = []
    owner: dict[str, str] = {}
    for role, value in table.items():
        if role not in ROLE_NAMES:
            problems.append((role, f"unknown role {role!r}; the roles are {', '.join(ROLE_NAMES)}"))
            continue
        if not _is_str_list(value):
            problems.append((role, f"role {role!r} must be a list of strings"))
            continue
        for name in value:
            if name in owner:
                problems.append((role, f"{name!r} is in role {owner[name]!r} and in {role!r}"))
            owner.setdefault(name, role)
        found[role] = tuple(dict.fromkeys(value))
    return found, problems


@cache
def _read(root: Path) -> dict[str, tuple[str, ...]]:
    path = root / MANIFEST
    try:
        document = tomllib.loads(GitRepo(root).load_text(MANIFEST))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise MeizelError(f"{path}: cannot read the roles: {error}") from error
    if "roles" not in document:
        raise MeizelError(f"{path}: has no [roles] table")
    found, problems = check_table(document["roles"])
    if problems:
        raise MeizelError(f"{path}: invalid [roles]: " + "; ".join(m for _, m in problems))
    return found


def roles(root: Path | None = None) -> Mapping[str, tuple[str, ...]]:
    """Role -> its top-level directories, in the order the root manifest lists them."""
    return _read((root or _ROOT).resolve())


def packages(*role_names: str, root: Path | None = None) -> tuple[str, ...]:
    """The top-level directories of the given roles, in the order given, each once."""
    return select_packages(roles(root), *role_names)


# meiva:note
# Select known roles from validated declarations without rereading their source.
def select_packages(declared: Mapping[str, tuple[str, ...]], *role_names: str) -> tuple[str, ...]:
    unknown = [name for name in role_names if name not in ROLE_NAMES]
    if unknown:
        raise MeizelError(f"unknown role(s) {unknown}; the roles are {', '.join(ROLE_NAMES)}")
    return tuple(dict.fromkeys(p for name in role_names for p in declared.get(name, ())))


def all_packages(root: Path | None = None) -> tuple[str, ...]:
    """Every declared top-level directory, by role order."""
    return packages(*ROLE_NAMES, root=root)


# meiva:note
# Test hosts owned by pytest's required `testpaths`, without a second registry.
# Supported roots are `tests` and `<host>/**/tests`; reject other patterns instead
# of silently giving pytest, the CI selector, and generated type checking different scopes.
def pytest_test_hosts(pyproject_text: str) -> tuple[str, ...]:
    document = tomllib.loads(pyproject_text)
    tool = document.get("tool")
    pytest_table = tool.get("pytest") if _is_table(tool) else None
    ini_options = pytest_table.get("ini_options") if _is_table(pytest_table) else None
    if not _is_table(ini_options) or "testpaths" not in ini_options:
        raise ValueError("pyproject.toml must configure tool.pytest.ini_options.testpaths")
    paths = ini_options["testpaths"]
    if not _is_object_list(paths) or not paths:
        raise ValueError("pytest testpaths must be a nonempty list of directory patterns")
    hosts: list[str] = []
    for pattern in paths:
        host = _pytest_test_host(pattern)
        if host in hosts:
            raise ValueError(f"duplicate pytest test host: {host!r}")
        hosts.append(host)
    return tuple(hosts)


def _pytest_test_host(pattern: object) -> str:
    if not isinstance(pattern, str):
        raise TypeError("pytest testpaths entries must be strings")
    if pattern == "tests":
        return pattern
    host, separator, tail = pattern.partition("/")
    if (
        not separator
        or tail != "**/tests"
        or host in ("", ".", "..", "tests")
        or any(char in host for char in "*?[]\\")
    ):
        raise ValueError(f"unsupported pytest testpaths pattern: {pattern!r}")
    return host
