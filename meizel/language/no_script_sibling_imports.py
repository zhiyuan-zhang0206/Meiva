# meiva:note
# The command-line entry of the PYTHONSAFEPATH sibling-import guard: it enumerates the files a run
# judges, reads them, states what sits beside each one, and prints what the rule found. Every
# decision — script mode, guards, sibling resolution, the repair — is in
# `meizel/language/script_siblings.py`, which reads nothing.
# meiva:link docs/conventions/testing.md

from __future__ import annotations

import sys
from pathlib import Path

from meizel import allowances, lint_common
from meizel.language import script_siblings

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PATH_IMPORT_SCOPE = allowances.load(_REPO_ROOT).names("path-imports", "scope")

__all__ = ["main", "scan_file"]


# meiva:note
# What one directory holds, as the rule reads it: the stems of its regular `.py` files and the names
# of its directories that carry an `__init__.py`. The two facts stay apart; which one wins is the
# rule's decision, not the order this listing came back in.
def _directory(path: Path) -> script_siblings.Directory:
    files: set[str] = set()
    packages: set[str] = set()
    for entry in path.iterdir():
        if (entry / "__init__.py").is_file():
            packages.add(entry.name)
        elif entry.suffix == ".py" and entry.is_file():
            files.add(entry.stem)
    return script_siblings.Directory(frozenset(files), frozenset(packages))


def scan_file(path: Path, *, root: Path | None = None) -> list[str]:
    """Return one error string per unguarded sibling import in a script-mode file."""
    repo_root = _REPO_ROOT if root is None else root
    src = lint_common.read_utf8_text(path)
    if src is None:
        return []
    try:
        rel = path.relative_to(repo_root).as_posix()
    except ValueError:
        # meiva:note
        # Not under the repository: pre-commit passes absolute paths, and an out-of-tree target is
        # judged by the same rule rather than crashing the run.
        rel = path.as_posix()
    return script_siblings.violations(rel, src, _directory(path.parent), scope=_PATH_IMPORT_SCOPE)


# meiva:note
# Every `.py` under the targets, or just the changed files a full walk would reach. The walk skips
# vendored and generated directories; the changed-files path applies the same rule without walking.
def _files_to_scan(
    resolved: list[str],
    targets: list[Path],
    scope: frozenset[str] | None,
    *,
    root: Path = _REPO_ROOT,
) -> list[Path]:
    if targets:
        return [
            path
            for entry in resolved
            if entry.endswith(".py")
            and (path := root / entry)
            and not any(
                script_siblings.excluded(path.relative_to(t).parts)
                for t in targets
                if path.is_relative_to(t)
            )
        ]
    if scope is None:
        return [
            p for p in root.rglob("*.py") if not script_siblings.excluded(p.relative_to(root).parts)
        ]
    return [
        root / rel
        for rel in scope
        if rel.endswith(".py") and not script_siblings.excluded(Path(rel).parts)
    ]


def main(argv: list[str] | None = None, *, repo_root: Path = _REPO_ROOT) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    argv, only = lint_common.split_only(argv)
    resolved: list[str] = []
    if argv:
        resolved, missing = lint_common.resolve_targets(argv, repo_root)
        if missing:
            print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
            return 1
    targets = [Path(a).resolve() for a in argv]
    total = 0
    for path in sorted(
        _files_to_scan(
            resolved, targets, lint_common.changed_scope(only, repo_root), root=repo_root
        )
    ):
        rel = (
            path.relative_to(repo_root).as_posix()
            if path.is_relative_to(repo_root)
            else path.as_posix()
        )
        if not script_siblings.judged(rel):
            continue
        for violation in scan_file(path, root=repo_root):
            total += 1
            print(violation)
    if total:
        print(
            f"\n{total} violation(s). See the header of meizel/language/no_script_sibling_imports.py "
            "for the guard pattern.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
