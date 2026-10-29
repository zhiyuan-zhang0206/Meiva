"""Forbid `import meiva` in the modules where graph hooks live.

Run: `.venv/bin/python scripts/lint/plugins/no_meiva_in_hooks.py [path ...]` (defaults to the
first-party packages; an explicit path that does not exist is an error). Also run by
pre-commit.

## Why

A graph hook runs in the agent host, a process serving many agents' turns. It operates on
the graph `state` it was handed and returns an update dict for LangGraph's reducer. The
`meiva` SDK is what agent code calls inside its exec child: its state slot (`meiva.state`)
exists only there, and its other state (identity, the exec turn) is per-process. A hook that
imports `meiva` reaches for SDK state that is not that agent's — or not there at all, which
is how the meiva_code after_exec hook read `meiva.state` in the host, got nothing, and silently
injected nothing. Keeping `meiva` out of hook modules makes the mistake impossible to write.

## Rule

A module is a hook module when it is any of:

- under `agent/hooks/`;
- named `agent_runtime.py` (a plugin's host-side face: its hooks and contributions);
- a module that defines a class deriving from `Hook` (`class X(Hook)` / `class X(hooks.Hook)`).

No import of `meiva` or `meiva.<anything>` may appear anywhere in it, including inside a
function or under `TYPE_CHECKING`. State a hook needs comes from its `state` argument
(`PluginStateHandle.view` / `.delta` for plugin state); an SDK-derived value it needs is
computed by a helper in a module that is not a hook module. There is no exemption and no
allowlist. Tests are not scanned.

Known gap: an `importlib.import_module("meiva...")` string or a module a hook module imports
that itself imports `meiva` is not seen — the rule is about the hook module's own imports.
Error format `file:line: <detail>`, non-zero exit.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from meizel import lint_common  # noqa: E402 - standalone script
from meizel.roles import packages  # noqa: E402 - standalone script

SCAN_DIRS = packages("application", "standalone", "templates", "examples")
_HOOKS_DIR = "agent/hooks/"
_FACE_NAME = "agent_runtime.py"


def _imports_meiva(node: ast.AST) -> str | None:
    """The offending import statement's text when `node` imports `meiva` (absolute), else None."""
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name == "meiva" or alias.name.startswith("meiva."):
                return f"import {alias.name}"
    elif (
        isinstance(node, ast.ImportFrom)
        and node.level == 0
        and node.module is not None
        and (node.module == "meiva" or node.module.startswith("meiva."))
    ):
        return f"from {node.module} import ..."
    return None


def _defines_hook(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            if (isinstance(base, ast.Name) and base.id == "Hook") or (
                isinstance(base, ast.Attribute) and base.attr == "Hook"
            ):
                return True
    return False


def _scan_file(path: Path, rel: str) -> list[tuple[int, str]]:
    """[(lineno, message), ...] for `meiva` imports in a hook module; empty for any other file."""
    source = lint_common.read_utf8_text(path)
    if source is None:
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []  # unreadable or broken files fail elsewhere; not this lint's job
    if not (rel.startswith(_HOOKS_DIR) or path.name == _FACE_NAME or _defines_hook(tree)):
        return []
    return [
        (
            node.lineno,
            f"`{statement}` in a hook module — hooks run in the agent host and operate on "
            "the graph `state` they are handed, never on the SDK",
        )
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        and (statement := _imports_meiva(node)) is not None
    ]


def main(argv: list[str] | None = None, *, repo_root: Path = _REPO_ROOT) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    argv, only = lint_common.split_only(argv)
    if argv:
        resolved, missing = lint_common.resolve_targets(argv, repo_root)
        if missing:
            print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
            return 1
        files = [repo_root / entry for entry in resolved if entry.endswith(".py")]
    else:
        roots = lint_common.scan_roots(repo_root, SCAN_DIRS)
        files = lint_common.python_files(roots, repo_root)

    scope = lint_common.changed_scope(only, repo_root)

    total = 0
    for path in sorted(lint_common.restrict(files, scope, repo_root)):
        if lint_common.is_repo_test_file(path, repo_root):
            continue
        try:
            rel = path.relative_to(repo_root).as_posix()
        except ValueError:
            rel = path.as_posix()
        for lineno, message in _scan_file(path, rel):
            total += 1
            print(f"{rel}:{lineno}: {message}")

    if total:
        print(
            f"\n{total} hook-module `meiva` import(s). Read the graph `state` argument "
            "(`PluginStateHandle.view` / `.delta`), or move the SDK-touching code into a "
            "module that defines no hook; see scripts/lint/plugins/no_meiva_in_hooks.py.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
