from __future__ import annotations

import ast
from collections.abc import Collection

from meizel import allowances, lint_common
from meizel.state.ambient_state import allowlist as allow
from meizel.state.ambient_state import bundle, scan
from meizel.state.ambient_state.tree import Tree

SECTION = "ambient_state"
# The rule module path used by integration-test repository fixtures.
LINT = "meizel/state/ambient_state/__init__.py"
Sites = dict[str, list[int]]

_STATE_FIX = (
    "inject what is read to decide: build it in a composition root and hand it to the "
    "component that uses it. A write-only facade (logger, meter, tracer) may stay "
    "global — list its file under [ambient-state.sink-facades] in meizel.policy.toml with a reason"
)
_BACKGROUND_FIX = (
    "background work must be durable or re-derivable from durable state and run as its own "
    "service loop; use a per-iteration `async with asyncio.TaskGroup()` for bounded "
    "parallelism; explicit tasks require retained Task registries, completion/error collection "
    "and finite teardown; threads require explicit admission, retained handles, stop, bounded join "
    "and completion/error collection (see Python conventions)"
)
_FIXES: dict[str, str] = {
    scan.INSTANCE: f"module-level instance — {_STATE_FIX}",
    scan.CONTAINER: f"module-level mutable container — {_STATE_FIX}",
    scan.CONTEXTVAR: (
        "pass the caller's runtime context explicitly as an argument instead of "
        "carrying dependencies through an ambient ContextVar"
    ),
    scan.GLOBAL_REBIND: f"a function rebinds a module global — {_STATE_FIX}",
    scan.FOREIGN: "rebinds an attribute of another module — give the owner a setter or inject the value",
    scan.CLASS_CONTAINER: "mutable container shared through a class attribute — make it per-instance state",
    scan.SINGLETON: f"a zero-argument cache is a hidden singleton — {_STATE_FIX}; a memoized pure derivation is listed under [ambient-state.sites] with a reason",
    scan.CACHE: "a memoized function is state unless it is pure — a pure derivation is listed under [ambient-state.sites] in meizel.policy.toml with a reason",
    scan.CALL: "a call that runs at import (a registry fill or side effect) — register from a composition root, not at import",
    bundle.BUNDLE_LEAK: f"a root bundle named outside its composition root — {bundle.FIX}",
    scan.READ: "reads settings, the environment, the clock or the filesystem at import — read it where it is used, or inject it",
    scan.HOST: "a platform constant computed at import — inject a `Platform` instead of recomputing the fact per module",
    scan.TASK: f"a free-floating task — {_BACKGROUND_FIX}",
    scan.THREAD: f"a free-floating thread — {_BACKGROUND_FIX}",
}


def in_scope(rel: str, scope: Collection[str]) -> bool:
    """Whether the rule governs this repo-relative path."""
    return (
        rel.split("/", 1)[0] in scope
        and not lint_common.is_test_path(rel)
        and not rel.endswith("__main__.py")
        and not any(rx.match(rel) for rx in allow.SKILL_SCRIPT_PATTERNS)
    )


_BACKGROUND_WORK = frozenset({scan.TASK, scan.THREAD})


def _is_facade_state(rel: str, hit: scan.Hit) -> bool:
    """State inside a sink facade is let through; background work started there is not."""
    return rel in allow.SINK_FACADES and hit.rule not in _BACKGROUND_WORK


def _site_key(rel: str, hit: scan.Hit) -> str:
    return f"{rel}::{hit.rule}:{hit.name}"


def _hits(tree: ast.Module, rel: str, index: Tree, scope: Collection[str]) -> list[scan.Hit]:
    if not in_scope(rel, scope):
        return []
    return [
        *scan.scan(tree, rel, index),
        *bundle.hits(tree, index),
    ]


def measure(tree: ast.Module, rel: str, index: Tree, scope: Collection[str]) -> Sites:
    """The site keys of one module with the closed lists applied, `path::rule:name -> [line]`."""
    sites: Sites = {}
    for hit in _hits(tree, rel, index, scope):
        key = _site_key(rel, hit)
        if key not in allow.ALLOWED and not _is_facade_state(rel, hit):
            sites.setdefault(key, []).append(hit.line)
    return sites


def _listed_in(rel: str) -> dict[str, str]:
    """The ALLOWED entries that name sites of this file."""
    prefix = f"{rel}::"
    return {key: why for key, why in allow.ALLOWED.items() if key.startswith(prefix)}


def allowlist_errors(
    tree: ast.Module, rel: str, index: Tree, scope: Collection[str]
) -> list[tuple[int, str]]:
    """A listed exemption whose site is no longer reported is stale."""
    listed = _listed_in(rel)
    if not listed and rel not in allow.SINK_FACADES:
        return []
    hits = _hits(tree, rel, index, scope)
    present = {_site_key(rel, hit) for hit in hits}
    errors = [
        (1, f"stale ambient_state list entry {key} — the site is gone; remove it from {_LIST_FILE}")
        for key in sorted(listed)
        if key not in present
    ]
    if rel in allow.SINK_FACADES and not any(_is_facade_state(rel, hit) for hit in hits):
        errors.append(
            (
                1,
                f"stale [ambient-state.sink-facades] entry — the file holds no module-level state; remove it from {_LIST_FILE}",
            )
        )
    return errors


_LIST_FILE = allowances.POLICY


def missing_allowlist_errors(index: Tree) -> list[str]:
    """A listed file or function that no longer exists is stale too."""
    listed_paths = {
        *allow.SINK_FACADES,
        *(key.partition("::")[0] for key in allow.ALLOWED),
    }
    errors = [
        f"{path}:1: stale ambient_state list entry — the file no longer exists; remove it from {_LIST_FILE}"
        for path in sorted(listed_paths)
        if not index.has_file(path)
    ]
    for callee in sorted(allow.PURE_REPO_CALLEES):
        owner, _, name = callee.rpartition(".")
        rel = index.module_path(owner)
        source = None if rel is None else index.read(rel)
        if source is None or f"def {name}(" not in source:
            errors.append(
                f"{_LIST_FILE}:1: stale [ambient-state.pure-repo-callees] entry {callee} — no such function; remove it"
            )
    return errors


# meiva:note
# The failure text for a new or grown site, or None when `target` is not a known `rule:name`.
def site_message(target: str) -> str | None:
    rule, separator, name = target.partition(":")
    if rule not in _FIXES or not (separator and name):
        return None
    return f"`{rule}` `{name}`: {_FIXES[rule]}"


def collect(
    tree: ast.Module,
    rel: str,
    index: Tree,
    sites: dict[str, Sites],
    scanned: set[str],
    scope: Collection[str],
) -> list[str]:
    """Add one module's measured sites to `sites` and mark it scanned; return its stale-list
    errors. The one call the gate makes per governed file."""
    sites[SECTION].update(measure(tree, rel, index, scope))
    scanned.add(rel)
    return [
        f"{rel}:{line}: {message}" for line, message in allowlist_errors(tree, rel, index, scope)
    ]
