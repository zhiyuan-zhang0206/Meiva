# meiva:note
# The command line: scan directories, target resolution, exit code. The only part
# that reads the world; the rule modules take what it hands down.
# meiva:link scripts/lint/docs/time-bomb-rule-parts.meiva.okf.md

from __future__ import annotations

import sys
from pathlib import Path

from meizel import checkout, lint_common, roles
from scripts.lint.diagnostics.time_bomb_rule.index import Index
from scripts.lint.diagnostics.time_bomb_rule.judgment import lint_source, lint_tests

_REPO_ROOT = Path(__file__).resolve().parents[4]
_ROLES = checkout.roles_at(_REPO_ROOT)
_SCAN_DIRS = roles.select_packages(_ROLES, "application", "standalone", "tooling")


@lint_common.admission.scoped
def main(argv: list[str], *, repo_root: Path | None = None) -> int:
    root = _REPO_ROOT if repo_root is None else repo_root.resolve()
    tree = checkout.source_tree(root)
    argv, only = lint_common.split_only(argv)
    scope = lint_common.changed_scope(tree, only)
    if argv:
        paths = [p if p.is_absolute() else root / p for p in (Path(a) for a in argv)]
        missing = lint_common.resolve_targets(tree, argv)[1]
        if missing:
            print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
            return 1
        used = {p for p in paths if p.is_dir()}
        dirs = _SCAN_DIRS if used else ()
        lint_common.scan_roots(tree, dirs)
        index = Index(root, dirs)
        errors = lint_source(index, tree, paths) + lint_tests(index, tree, paths)
    else:
        dirs = _SCAN_DIRS
        index = Index(root, dirs)
        errors = lint_source(index, tree, lint_common.scan_roots(tree, dirs), scope) + lint_tests(
            index, tree, [root / "tests", *lint_common.scan_roots(tree, dirs)], scope
        )
    for err in errors:
        print(err, file=sys.stderr)
    return 1 if errors else 0
