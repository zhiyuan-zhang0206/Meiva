# meiva:note
# The command-line entry of the env-presence rule: it lists the files the rule judges, reads them and
# prints what `scripts/rules/state/env_presence.py` found. Python outside the tests is judged by its
# syntax tree; shell scripts and the GitHub workflow and action files by their text. `--only` judges
# the changed files under the same scope. A file listed under `[env-presence] files` in the policy is
# skipped, and a listed file the rule no longer reports in fails the full scan as stale.
# meiva:link scripts/rules/docs/rules.md
from __future__ import annotations

import ast
import sys
from pathlib import Path

from meizel.api import GitRepo, SourceTree, allowances, lint_common, source_tree

from scripts.rules.state import env_presence

_SHELL_SUFFIXES = (".sh", ".bash")
_WORKFLOW_DIRS = (".github/workflows/", ".github/actions/")


# meiva:note
# Whether the rule judges `rel`, and as which language: production Python, a shell script, or a
# workflow or action file whose `run:` blocks are shell text.
def _language(rel: str) -> str | None:
    if rel.endswith(".py"):
        return None if lint_common.is_test_path(rel) else "python"
    if rel.endswith(_SHELL_SUFFIXES):
        return "shell"
    if rel.startswith(_WORKFLOW_DIRS) and rel.endswith((".yml", ".yaml")):
        return "shell"
    return None


def _findings(language: str, text: str) -> list[tuple[int, str]]:
    if language == "shell":
        return env_presence.shell_findings(text)
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    return env_presence.python_findings(tree)


def _report(tree: SourceTree, root: Path, rels: list[str], exempt: dict[str, str]) -> list[str]:
    errors: list[str] = []
    for rel in sorted(rels):
        language = _language(rel)
        if language is None or rel in exempt:
            continue
        text = lint_common.read_utf8_text(tree, root / rel)
        if text is None:
            continue
        errors.extend(f"{rel}:{line}: {message}" for line, message in _findings(language, text))
    return errors


# meiva:note
# The listed files the rule no longer reports: an exemption outlives its reason once the file is
# gone or reads its variables by value.
def _stale(tree: SourceTree, root: Path, exempt: dict[str, str]) -> list[str]:
    stale: list[str] = []
    for rel in sorted(exempt):
        language = _language(rel)
        text = lint_common.read_utf8_text(tree, root / rel) if (root / rel).is_file() else None
        if language is None or text is None or not _findings(language, text):
            stale.append(
                f"{allowances.POLICY}: stale [{env_presence.RULE}] files entry {rel}: "
                "the file no longer tests a variable's presence"
            )
    return stale


def main(argv: list[str], *, repo_root: Path) -> int:
    tree = source_tree(repo_root)
    exempt = allowances.load(GitRepo(repo_root)).reasons(env_presence.RULE, "files")
    argv, only = lint_common.split_only(argv)
    if argv:
        rels, missing = lint_common.resolve_targets(tree, argv)
        if missing:
            print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
            return 1
    else:
        scope = lint_common.changed_scope(tree, only, inputs=(allowances.POLICY,))
        rels = sorted(scope) if scope is not None else tree.tracked(regular=True)
    errors = _report(tree, repo_root, rels, exempt)
    if not argv and only is None:
        errors.extend(_stale(tree, repo_root, exempt))
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], repo_root=Path.cwd()))
