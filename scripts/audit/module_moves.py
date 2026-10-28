"""Audit module moves without compatibility shims across repository content.

Run: `.venv/bin/python scripts/audit/module_moves.py OLD=NEW [OLD=NEW ...]`

## Why

Moving modules into packages reduces directory entries only if the old files
disappear. Unlike a module split with re-exports, a move must leave no callers
at the old path, and every name referenced at the new path must still exist.
Before the move, `where_used.py` lists everything it will touch.

## What it checks

For each pair, report old-path references with file:line locations, then import
the new module and check names from `from NEW import X`, `NEW.X`, and quoted
targets. Comments, documentation, and non-Python text are included. Wildcard
imports cannot enumerate their references and fail the completeness check.
`git show <rev>:<path>` operands are object reads of pinned history, so they
are blanked before matching.

The invariant is that committed repository content carries no reference to the
old path. Scan working-tree text for `git ls-files` entries, excluding the frozen
docs/decisions/ and docs/postmortems/ axes and this script itself. Untracked
environments, caches, and operator scaffolding are outside that universe. Stage
new files before auditing. Binary files are skipped. Any failed check exits 1.

This audit checks references and importable names, not pytest fixture resolution.
For a test move, attach before/after runtime fixture closure evidence to review:
valid declarations alone cannot detect an omitted destination autouse fixture.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

from meizel import lint_common
from scripts.audit.analysis import (
    is_text,
    module_pair,
    old_references,
    pair_result,
    referenced_names,
    verdict_line,
)
from scripts.audit.analysis.frozen import is_frozen

__all__ = ["main"]

_REPO = Path(__file__).resolve().parent.parent.parent


def _is_excluded(name: str) -> bool:
    """Whether a tracked path is outside the audit's live-content universe."""
    return not name or is_frozen(name)


def _tracked_text(repo_root: Path) -> list[tuple[str, str]]:
    paths = lint_common.tracked_files(repo_root)
    texts: list[tuple[str, str]] = []
    for name in paths:
        if _is_excluded(name):
            continue
        path = repo_root / name
        if path.resolve() == Path(__file__).resolve():
            continue
        # Git tracks a symlink's target path, not the target file's content.
        text = path.readlink().as_posix() if path.is_symlink() else lint_common.read_utf8_text(path)
        if not is_text(text):
            continue
        texts.append((name, text))
    return texts


def _missing(module: str, refs: set[str]) -> list[str]:
    """Names a caller can still reach at `module`.

    A reference resolves when it is an attribute of the module **or** a
    submodule of it: `from <pkg> import <sub>` is Python's documented
    fallback, so a package whose `__init__` stays lean (no eager submodule
    re-exports) is not a broken move.
    """
    loaded = importlib.import_module(module)
    missing: list[str] = []
    for name in sorted(refs):
        if hasattr(loaded, name):
            continue
        try:
            importlib.import_module(f"{module}.{name}")
        except ImportError:
            missing.append(name)
    return missing


def main(argv: list[str], *, repo_root: Path | None = None) -> int:
    """Audit tracked content in an explicit checkout, defaulting to this checkout.

    New module imports use the caller's Python import environment, as in the CLI.
    Diagnostics and exit codes are shared by command-line and direct callers.
    """
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("## What it checks")[0])
    parser.add_argument("pairs", nargs="+", metavar="OLD=NEW", type=module_pair)
    args = parser.parse_args(argv)
    texts = _tracked_text(_REPO if repo_root is None else repo_root.resolve())
    failed = False
    for old, new in args.pairs:
        old_count = 0
        refs: set[str] = set()
        for path, text in texts:
            lines = old_references(old, text)
            old_count += len(lines)
            for line in lines:
                print(f"{path}:{line}: old reference to {old}")
            refs.update(referenced_names(new, text))
        import_error = ""
        missing: list[str] = []
        try:
            missing = _missing(new, refs)
        except Exception as exc:
            # An import failure is an audit failure; still report the remaining pairs.
            import_error = f"; import failed: {type(exc).__name__}: {exc}"
        line, pair_failed = pair_result(
            old,
            new,
            old_count=old_count,
            names=len(refs),
            missing=missing,
            import_error=import_error,
        )
        print(line)
        failed = failed or pair_failed
    print(verdict_line(failed=failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
