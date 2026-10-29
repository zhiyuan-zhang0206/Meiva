#!/usr/bin/env python3
"""Whether a pull request can skip the e2e jobs: the rule is written here, not decided per run.

e2e is the integration net for product behavior, so it runs whenever either side changed. A
change that only edits unit tests inside packages (`<package>/**/tests/**/*.py`) cannot change
product behavior, and the backend selection already ran every test that can observe the edited
tests. The rule skips e2e exactly when all of these hold, with no further judgment:

- the event is a pull request, and not a Trunk queue branch (the queue always runs everything);
- test selection is enforced and its decision is SELECTED;
- every changed path is a Python file in a package-internal `tests` directory;
- none of those paths is one the e2e suite reaches by import (`e2e_reached_unit_tests`); a walk
  that cannot understand a file or an import form fails closed and never skips.

Top-level `tests/` is out of scope on purpose: e2e imports `tests.fixtures`, `tests.components`
and its own helpers, so a change there can reach e2e. A few test helpers live beside the
package that owns them and are shared with e2e (a polling helper, a login seeder); the rule
computes that set from e2e's own imports instead of assuming it is empty, so editing one of
them still runs e2e.

Standalone and stdlib-only: the classify job runs it without an environment.
"""

# meiva:note
# The rule widened past the list above: a changed path also qualifies when it is tooling, an
# application module whose interface the diff leaves unchanged, a locality manifest, or prose
# under `docs/` that is not an OKF bundle. `docs/conventions/engineering/test-selection.md` is the
# statement to read; the list above predates it.

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections.abc import Iterable
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from meizel.checkout import roles_at  # noqa: E402
from meizel.roles import select_packages  # noqa: E402

ROLES = roles_at(_REPOSITORY_ROOT)

# The packages that carry their own `tests` directories: the application packages and the
# repository's own tooling (not `tests`, `ui`, the schedule templates or the demos).
APPLICATION_PACKAGES = select_packages(ROLES, "application")
TOOLING_PACKAGES = select_packages(ROLES, "tooling")
PACKAGES = (*APPLICATION_PACKAGES, *TOOLING_PACKAGES)
_PACKAGE_UNIT_TEST = re.compile(rf"(?:{'|'.join(PACKAGES)})/(?:[^/]+/)*tests/(?:[^/]+/)*[^/]+\.py")
_TOOLING_PYTHON = re.compile(rf"(?:{'|'.join(TOOLING_PACKAGES)})/(?:[^/]+/)*[^/]+\.py")
_APPLICATION_PYTHON = re.compile(rf"(?:{'|'.join(APPLICATION_PACKAGES)})/(?:[^/]+/)*[^/]+\.py")
_QUEUE_PREFIXES = ("trunk-merge/", "trunk-temp/")
_MANIFEST_NAME = "meizel.toml"
_DOCUMENT_SUFFIX = ".md"
_DOCUMENTS_DIRECTORY = "docs"
_OKF_DOCUMENT = ".meiva.okf.md"


def is_package_unit_test(path: str) -> bool:
    """Whether a repo-relative path is a Python file under a package-internal `tests` directory."""
    return _PACKAGE_UNIT_TEST.fullmatch(path) is not None


def is_tooling_python(path: str) -> bool:
    return _TOOLING_PYTHON.fullmatch(path) is not None


# meiva:note
# A manifest carries the directory's exports, declared edges and recorded debt; no runtime reads
# it. Selected by file kind rather than by directory, so every `meizel.toml` in the tree counts.
def is_locality_manifest(path: str) -> bool:
    return path.rsplit("/", 1)[-1] == _MANIFEST_NAME


# meiva:note
# Prose is a `.md` inside a `docs` directory, and never an OKF bundle file: the gateway rebuilds
# `GET /api/okf/graph` from the live `.meiva.okf.md` tree on every request, so a bundle edit can
# break that page. Every other `.md` stays out too - a skill file is read into the model's prompt
# by the runtime skill loader, so editing one changes what ships.
def is_documentation(path: str) -> bool:
    return (
        path.endswith(_DOCUMENT_SUFFIX)
        and not path.endswith(_OKF_DOCUMENT)
        and _DOCUMENTS_DIRECTORY in path.split("/")[:-1]
    )


# Calls that import by name or by path. A literal module name is resolved like an import
# statement; anything else (a computed name, a path) is not understood, so it fails closed.
_NAMED_IMPORTS = {"import_module", "__import__", "run_module", "find_spec", "load_module"}
_UNDERSTOOD_NOTHING = {"run_path", "spec_from_file_location", "exec", "eval", "load_source"}


# The dynamic imports e2e is known to make, each read by a person: they run a product module or a
# generated program given on the command line, never a repository test file. Any other site fails
# closed, and `test_the_real_e2e_walk_understands_every_import` keeps this list honest.
_KNOWN_DYNAMIC: dict[str, frozenset[str]] = {
    "tests/e2e/process_support.py": frozenset({"run_module"}),  # the served daemon module
    "tests/e2e/fakes/child_budget_writer.py": frozenset({"exec"}),  # the generated scenario program
}


def stale_known_dynamic(root: Path, known: dict[str, frozenset[str]] | None = None) -> list[str]:
    """The `_KNOWN_DYNAMIC` entries whose call is gone: the file is missing or unparsable, or no
    longer makes a call of that name. A stale entry would quietly excuse a new site of the same
    name, so a test requires this to be empty and the entry gets deleted with the call."""
    stale: list[str] = []
    for relative, names in sorted((_KNOWN_DYNAMIC if known is None else known).items()):
        try:
            tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        except (SyntaxError, ValueError, OSError):
            stale.extend(
                f"{relative}: {name} (file missing or unreadable)" for name in sorted(names)
            )
            continue
        called = {_call_name(node) for node in ast.walk(tree) if isinstance(node, ast.Call)}
        stale.extend(f"{relative}: {name}" for name in sorted(names) if name not in called)
    return stale


class _NotUnderstoodError(Exception):
    """A file e2e reaches that this rule cannot read or follow; the answer must be 'reached'."""


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _from_import_modules(node: ast.ImportFrom, package: tuple[str, ...], file: Path) -> list[str]:
    """The modules one `from ... import ...` names, a relative one resolved against `package`."""
    if node.level:
        if node.level > len(package) + 1:
            raise _NotUnderstoodError(f"{file}: a relative import climbs above the package")
        anchor = list(package[: len(package) - (node.level - 1)])
        module = ".".join([*anchor, *(node.module.split(".") if node.module else [])])
    else:
        module = node.module or ""
    return [module, *(f"{module}.{alias.name}" for alias in node.names)]


def _call_import(node: ast.Call, file: Path, root: Path) -> list[str]:
    """The module a call imports by name; raises for a call whose import cannot be read."""
    name = _call_name(node)
    if name in _KNOWN_DYNAMIC.get(file.relative_to(root).as_posix(), frozenset()):
        return []
    if name in _UNDERSTOOD_NOTHING:
        raise _NotUnderstoodError(
            f"{file}:{node.lineno}: {name}() imports what a reader cannot see"
        )
    if name not in _NAMED_IMPORTS:
        return []
    argument = node.args[0] if node.args else None
    if not (isinstance(argument, ast.Constant) and isinstance(argument.value, str)):
        raise _NotUnderstoodError(f"{file}:{node.lineno}: {name}() with a computed module name")
    return [argument.value]


def _imported_modules(tree: ast.AST, file: Path, root: Path) -> Iterable[str]:
    """Every module `tree` imports; raises `_NotUnderstoodError` for a form it cannot resolve."""
    package = file.relative_to(root).with_suffix("").parts[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            yield from _from_import_modules(node, package, file)
        elif isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.Call):
            yield from _call_import(node, file, root)


def e2e_reached_unit_tests(root: Path) -> frozenset[str] | None:
    """Package-internal test files the e2e suite imports, directly or through other test modules.

    The walk starts at every `tests/e2e` file and follows imports into package-internal `tests`
    directories and into the top-level `tests/` helpers (e2e imports `tests.fixtures`, which in
    turn imports package test helpers). An edit to a file it finds can change what e2e does
    even though it sits in a package `tests` directory, so it never qualifies for the skip.

    Fail closed: a file that cannot be read or parsed, a computed or path-based import, or a
    relative import that cannot be resolved returns `None`, which means "assume everything is
    reached" and never skips e2e.
    """
    reached: set[str] = set()
    pending = sorted((root / "tests" / "e2e").rglob("*.py"))
    seen: set[Path] = set()
    try:
        while pending:
            file = pending.pop()
            if file in seen:
                continue
            seen.add(file)
            try:
                tree = ast.parse(file.read_text(encoding="utf-8"))
            except (SyntaxError, ValueError, OSError) as error:
                raise _NotUnderstoodError(f"{file}: {error}") from error
            for module in _imported_modules(tree, file, root):
                if module.split(".")[0] not in (*PACKAGES, "tests"):
                    continue
                base = Path(*module.split("."))
                for candidate in (base.with_suffix(".py"), base / "__init__.py"):
                    relative = candidate.as_posix()
                    if not (root / candidate).is_file():
                        continue
                    if is_package_unit_test(relative) or is_tooling_python(relative):
                        reached.add(relative)
                    if (
                        is_package_unit_test(relative)
                        or is_tooling_python(relative)
                        or relative.startswith("tests/")
                    ):
                        pending.append(root / candidate)
    except _NotUnderstoodError:
        return None
    return frozenset(reached)


def interface_unchanged_paths(
    changed: list[str], *, repo_root: Path, base_ref: str | None = None
) -> frozenset[str]:
    if base_ref is None or not changed:
        return frozenset()
    candidates = [
        path for path in changed if not is_package_unit_test(path) and not is_tooling_python(path)
    ]
    if not candidates or not all(_APPLICATION_PYTHON.fullmatch(path) for path in candidates):
        return frozenset()
    from scripts.ci.test_impact import (
        base_checkout,
        build_impact,
        qualifies_for_interface_cutoff,
    )
    from scripts.ci.test_selector import PathClass, classify_path, load_checkout

    head = load_checkout(repo_root)
    head_impact = build_impact(head.repo_root, head.collectable)
    with base_checkout(head.repo_root, base_ref) as base_root:
        base = load_checkout(base_root)
        base_impact = build_impact(base_root, base.collectable & head.collectable)
        return frozenset(
            path
            for path in candidates
            if classify_path(path, head) is PathClass.PACKAGE
            and classify_path(path, base) is PathClass.PACKAGE
            and qualifies_for_interface_cutoff(
                path,
                base=base,
                head=head,
                base_impact=base_impact,
                head_impact=head_impact,
            )
        )


# meiva:note
# One changed path the e2e suite cannot observe a difference through: a test module, tooling, an
# application module whose interface the diff leaves unchanged, a locality manifest, or prose.
def _cannot_change_behavior(path: str, interface_unchanged: frozenset[str]) -> bool:
    return (
        is_package_unit_test(path)
        or is_tooling_python(path)
        or path in interface_unchanged
        or is_locality_manifest(path)
        or is_documentation(path)
    )


def skips_e2e(
    *,
    event: str,
    head_ref: str,
    mode: str,
    decision: str,
    changed: list[str],
    reached: frozenset[str] | None = frozenset(),
    interface_unchanged: frozenset[str] = frozenset(),
) -> bool:
    """The whole rule: see the module docstring. `reached` is `e2e_reached_unit_tests`;
    `None` (the walk could not understand something) never skips."""
    return (
        event == "pull_request"
        and not head_ref.startswith(_QUEUE_PREFIXES)
        and mode == "enforce"
        and decision == "SELECTED"
        and bool(changed)
        and all(_cannot_change_behavior(path, interface_unchanged) for path in changed)
        and reached is not None
        and reached.isdisjoint(changed)
    )


def main(argv: list[str] | None = None) -> int:
    """Print `skip=true` or `skip=false` for the workflow's step output."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changed-files", type=Path, required=True)
    parser.add_argument("--event", required=True)
    parser.add_argument("--head-ref", default="")
    parser.add_argument("--mode", default="")
    parser.add_argument("--decision", default="")
    parser.add_argument("--base-ref", default=None)
    parser.add_argument("--repo-root", type=Path, default=_REPOSITORY_ROOT)
    args = parser.parse_args(argv)
    changed = [line.strip() for line in args.changed_files.read_text().splitlines() if line.strip()]
    repo_root = args.repo_root.resolve()
    skip = skips_e2e(
        event=args.event,
        head_ref=args.head_ref,
        mode=args.mode,
        decision=args.decision,
        changed=changed,
        reached=e2e_reached_unit_tests(repo_root),
        interface_unchanged=interface_unchanged_paths(
            changed, repo_root=repo_root, base_ref=args.base_ref
        ),
    )
    print(f"skip={str(skip).lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
