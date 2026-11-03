#!/usr/bin/env python
# meiva:note
# Generate the pyright `executionEnvironments` entries that keep a tests directory out of its
# covering entry's Any rules while its Any debt is unpaid.
#
# The seven `reportUnknown*`/`reportMissing*` rules are global errors, so no entry is needed for
# them. A hand-written directory entry that also holds `reportAny` / `reportExplicitAny` (such as
# `gateway/routers`) covers every file under its root, tests directories included, and pyright
# takes the FIRST entry whose root (a plain directory, no globs) contains a file. A tests
# directory under such a root therefore needs an entry of its own, listed before the covering
# entry, to opt out of its Any rules until its tests have paid that debt.
#
# This script owns those entries. `TESTS_OWING_ANY` lists the path prefixes of tests that still owe
# Any debt; for every tracked `<pkg>/**/tests/` directory (read from the test hosts of pytest
# `testpaths`) under a listed prefix whose covering entry holds the Any rules it writes one entry
# into the region of `pyproject.toml` fenced by the BEGIN/END comments below, carrying the
# covering entry's other settings (`extraPaths` and the like) and no Any rule. Everything outside the region is
# hand-written and left byte-for-byte alone. The region is sorted by root, one block per
# directory, so two changes collide only where their roots are neighbors in that order; run this
# script again to resolve.
#
# Add a prefix to the list in the pull request that adds the covering entry; remove it when its
# tests are clean (the ratchet only tightens). This script, its hook and its tests go when
# the last root leaves the list, that is when `reportAny` is a global error.
#
# Run after adding, moving or removing a tests directory under a listed root, or let the
# pre-commit `lint-pyright-test-environments` hook fail loud:
#
#     .venv/bin/python scripts/codegen/gen_pyright_test_environments.py
#
# `--check` exits 1 without writing when the region is stale.
#

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# meiva:note
# A pre-commit hook runs this file, so it enters a scratch home before its first application import
# (`scripts/tests/test_hooks_scratch_home.py`); the imports that follow stand on the checkout root put
# on `sys.path` above.
from base.host.env.boot_actions import enter_scratch_home  # noqa: E402

if __name__ == "__main__":
    enter_scratch_home()

from base.host.boundary.fields import str_field  # noqa: E402
from base.host.boundary.json_values import is_object_dict, is_object_list  # noqa: E402
from base.host.boundary.options import options  # noqa: E402
from base.host.boundary.untyped import object_of  # noqa: E402
from meizel.roles import pytest_test_hosts  # noqa: E402

BEGIN = (
    "# BEGIN GENERATED pyright tests environments "
    "(scripts/codegen/gen_pyright_test_environments.py; edit nothing between the markers)"
)
END = "# END GENERATED pyright tests environments"

# meiva:note
# The two basedpyright rules a package holds at `error` once its Any debt is repaid. They gate
# production code only: a tests directory under a package entry that sets them does not repeat
# them, so its tests stay at the global level until their own Any debt is repaid.
ANY_RULES = ("reportAny", "reportExplicitAny")
# meiva:note
# Path prefixes of tests that have not repaid their Any debt while the entry covering them holds the
# Any rules in production code. A tests directory under a listed prefix gets a generated entry only
# when its covering entry holds those rules (otherwise there is nothing to opt out of). Add the
# prefix in the pull request that adds the covering entry; remove it when the tests are clean.
TESTS_OWING_ANY: tuple[str, ...] = (
    "base/agents",
    "cli",
    "gateway/tests",
    "meiva_builtins/skills/coordination",
    "meiva_builtins/skills/platform/meiva-guide",
    "meiva_builtins/skills/platform/meiva-self-evolution",
    "scripts/ci",
    "scripts/host_ops",
    "scripts/lint",
    "scripts/model_registry",
    "scripts/structure",
    "scripts/verify",
    "services/derived",
    "services/desktop",
    "services/entrypoints",
    "services/supervision",
)


def _covers(root: str, directory: str) -> bool:
    return directory == root or directory.startswith(f"{root}/")


# meiva:note
# The first `executionEnvironments` entry whose root contains `directory`, else {}.
def covering_environment(config: Mapping[str, object], directory: str) -> dict[str, object]:
    raw = config.get("executionEnvironments", [])
    if not is_object_list(raw) or not all(is_object_dict(env) for env in raw):
        raise SystemExit("[tool.pyright].executionEnvironments must be an array of tables")
    environments = [env for env in raw if is_object_dict(env)]
    none: dict[str, object] = {}
    return next((env for env in environments if _covers(str_field(env, "root"), directory)), none)


def tests_roots(tracked: Iterable[str], hosts: tuple[str, ...]) -> list[str]:
    """The outermost `tests/` directory of every tracked module in a package, sorted."""
    roots: set[str] = set()
    for path in tracked:
        parts = path.split("/")
        if parts[0] != "tests" and parts[0] in hosts and "tests" in parts[:-1]:
            roots.add("/".join(parts[: parts.index("tests") + 1]))
    return sorted(roots)


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_str_list(value: object) -> TypeGuard[list[str]]:
    return _is_object_list(value) and all(isinstance(item, str) for item in value)


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)
    if _is_str_list(value):
        return json.dumps(value)
    raise SystemExit(
        f"cannot repeat a {type(value).__name__} setting in a generated entry: {value!r}"
    )


# meiva:note
# The non-rule settings of the entry covering `root` (`extraPaths` and the like).
def _inherited(config: Mapping[str, object], root: str) -> dict[str, object]:
    return {
        key: value
        for key, value in covering_environment(config, root).items()
        if key != "root" and key not in ANY_RULES
    }


# meiva:note
# One entry per tests directory that sits under a prefix in `owing_any` and whose covering entry
# holds the Any rules: the covering entry holds them for production code, and the tests under it
# have not repaid theirs. The entry repeats the covering entry's other settings and leaves the Any
# rules out.
def _entries(
    config: Mapping[str, object], roots: list[str], *, owing_any: Collection[str]
) -> list[dict[str, object]]:
    return [
        {"root": root, **_inherited(config, root)}
        for root in roots
        if any(_covers(prefix, root) for prefix in owing_any)
        and covering_environment(config, root).keys() & set(ANY_RULES)
    ]


def _render(entries: list[dict[str, object]]) -> str:
    blocks = [
        "[[tool.pyright.executionEnvironments]]\n"
        + "".join(f"{key} = {_toml_value(value)}\n" for key, value in entry.items())
        + "\n"
        for entry in entries
    ]
    return f"{BEGIN}\n{''.join(blocks)}{END}\n"


def _region(text: str) -> re.Match[str]:
    if text.count(BEGIN) != 1 or text.count(END) != 1:
        raise SystemExit(
            "pyproject.toml must contain exactly one generated region: a line "
            f"{BEGIN!r} followed by a line {END!r}"
        )
    match = re.search(rf"^{re.escape(BEGIN)}\n.*?^{re.escape(END)}\n", text, re.S | re.M)
    if match is None:
        raise SystemExit("the generated region markers of pyproject.toml are out of order")
    return match


def generate(
    pyproject_text: str,
    tracked: Iterable[str],
    *,
    owing_any: Collection[str] = TESTS_OWING_ANY,
) -> str:
    """`pyproject_text` with its generated region rewritten for the tracked tests directories."""
    region = _region(pyproject_text)
    handwritten = pyproject_text[: region.start()] + pyproject_text[region.end() :]
    document = object_of(tomllib.loads, handwritten)
    tool = document.get("tool") if is_object_dict(document) else None
    config = tool.get("pyright") if is_object_dict(tool) else None
    if not is_object_dict(config):
        raise SystemExit("pyproject.toml has no [tool.pyright] table")
    roots = tests_roots(tracked, pytest_test_hosts(handwritten))
    rendered = _render(_entries(config, roots, owing_any=owing_any))
    return pyproject_text[: region.start()] + rendered + pyproject_text[region.end() :]


def _tracked_python_files(repo_root: Path) -> list[str]:
    result = subprocess.run(  # noqa: S603 - fixed git query in a repository
        [
            "env",
            "-u",
            "GIT_DIR",
            "-u",
            "GIT_WORK_TREE",
            "-u",
            "GIT_INDEX_FILE",
            "-u",
            "GIT_COMMON_DIR",
            "git",
            "-C",
            str(repo_root),
            "ls-files",
            "-z",
            "--",
            "*.py",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return [path for path in result.stdout.split("\0") if path]


@dataclass(frozen=True)
class Options:
    check: bool


def main(
    argv: list[str],
    *,
    repo_root: Path = _REPO_ROOT,
    owing_any: Collection[str] = TESTS_OWING_ANY,
) -> int:
    parser = argparse.ArgumentParser(
        description="Generate the pyright executionEnvironments entries for tests directories under roots that owe Any debt."
    )
    parser.add_argument(
        "--check", action="store_true", help="exit 1 if pyproject.toml is stale; write nothing"
    )
    args = options(parser.parse_args(argv), Options)
    pyproject = repo_root / "pyproject.toml"
    current = pyproject.read_text(encoding="utf-8")
    fresh = generate(current, _tracked_python_files(repo_root), owing_any=owing_any)
    if fresh == current:
        return 0
    if args.check:
        print(
            "pyproject.toml: the pyright tests environments do not match the tests directories.\n"
            "Run .venv/bin/python scripts/codegen/gen_pyright_test_environments.py to regenerate.",
            file=sys.stderr,
        )
        return 1
    pyproject.write_text(fresh, encoding="utf-8")
    print("pyproject.toml: pyright tests environments regenerated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
