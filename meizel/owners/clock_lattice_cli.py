# meiva:note
# The command-line entry of the clock-lattice rule: it reads the roles this repository declares, the
# policy, the family modules' text and the checkout's file set once, and hands those values to
# `meizel/owners/clock_lattice.py`, which opens nothing. A stale local hook or script that starts
# this rule's module instead reads a refusal naming this shell, never an empty pass.

from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from pathlib import Path

from meizel import allowances, lint_common
from meizel.checkout import roles_at
from meizel.owners import clock_lattice
from meizel.roles import select_packages

_REPO_ROOT = Path(__file__).parents[2]
SCAN_DIRS = select_packages(roles_at(_REPO_ROOT), "application", "standalone", "tooling")

__all__ = ["SCAN_DIRS", "lattice_of", "main", "scan_file", "stale_entries"]

# meiva:note
# pytest owns a test file's path insertion, so a test file is never a lattice site.
_TEST_PATTERNS = (
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(r"_test\.py$"),
)


# meiva:note
# The lattice a checkout declares: the policy's two tables, and the `CLOCKS` tables of the family
# modules it names, read here because the rule may not read.
def lattice_of(root: Path) -> clock_lattice.Lattice:
    policy = allowances.load(root)
    family = tuple(policy.names("clock-lattice", "family-modules"))
    return clock_lattice.Lattice.of(policy, _texts(root, family))


def _texts(root: Path, relatives: Iterable[str]) -> dict[str, str]:
    return {
        rel: text
        for rel in relatives
        if (text := lint_common.read_utf8_text(root / rel)) is not None
    }


def _rel_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


# meiva:note
# One file judged from what is on disk: the name the rule reports, its text, and the lattice.
def scan_file(path: Path, *, repo_root: Path, lattice: clock_lattice.Lattice) -> list[str]:
    source = lint_common.read_utf8_text(path)
    if source is None:
        return []
    return clock_lattice.scan_source(_rel_path(path, repo_root), source, lattice=lattice)


def _scan(
    paths: list[Path], scope: frozenset[str] | None, *, root: Path, lattice: clock_lattice.Lattice
) -> list[str]:
    errors: list[str] = []
    for path in paths:
        for p in lint_common.python_files([path], root):
            rel = _rel_path(p, root)
            if any(pattern.search(rel) for pattern in _TEST_PATTERNS):
                continue
            if scope is not None and rel not in scope:
                continue
            errors.extend(scan_file(p, repo_root=root, lattice=lattice))
    return errors


# meiva:note
# The stale-allowlist pass: the family modules and every exempted file must still exist, and an
# exemption must still name something the file binds. Only the files the lattice names are asked
# about, so a scan of a tree that is not a checkout still answers.
def stale_entries(root: Path, lattice: clock_lattice.Lattice) -> list[str]:
    wanted = {*lattice.family_modules, *(rel for rel, _ in lattice.independent)}
    existing = {rel for rel in wanted if (root / rel).is_file()}
    return clock_lattice.stale_allowlist_entries(existing, _texts(root, existing), lattice=lattice)


@lint_common.admission.scoped
def main(
    argv: list[str] | None = None,
    *,
    scan_dirs: tuple[str, ...],
    repo_root: Path | None = None,
    lattice: clock_lattice.Lattice | None = None,
) -> int:
    root = _REPO_ROOT if repo_root is None else repo_root
    lattice = lattice_of(root) if lattice is None else lattice
    argv = list(sys.argv[1:] if argv is None else argv)
    argv, only = lint_common.split_only(argv)
    if argv:
        resolved, missing = lint_common.resolve_targets(argv, root)
        if missing:
            print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
            return 1
        paths = [root / entry for entry in resolved if entry.endswith(".py")]
    else:
        paths = lint_common.scan_roots(root, scan_dirs)
    # meiva:note
    # `--only` (the commit hook) judges the changed files under the default scope; the lattice's own
    # family modules are inputs: editing one re-judges every file.
    scope = lint_common.changed_scope(only, root, inputs=lattice.family_modules)
    errors = _scan(paths, scope, root=root, lattice=lattice)
    if not argv:
        errors.extend(stale_entries(root, lattice))
    for err in errors:
        print(err, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main(scan_dirs=SCAN_DIRS))
