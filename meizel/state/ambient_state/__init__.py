from __future__ import annotations

import ast
from collections.abc import Collection
from dataclasses import dataclass

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
# meiva:note
# The document that states the rule; the message of this owner points here.
_DOC = "meizel/docs/ambient-state.meiva.okf.md"

# meiva:note
# Why the rule exists, per mechanism rather than per rule: the rules that report the same kind of
# dependency share one reason, written once, and a mechanism that is genuinely different gets its
# own. Each reason also says what the site that adds it now owns, so the message teaches instead of
# only forbidding.
_STATE_WHY = (
    "state that lives on the module is built before anything can configure it and is visible to "
    "every reader of it, so no caller or test can hand in a different value and what this code "
    "does depends on what ran before it — whoever adds a site here owns that value's lifetime and "
    "how a test replaces it"
)
_CONTEXT_WHY = (
    "a dependency carried in ambient context disappears from the signature, so neither the caller "
    "nor the reader can see what the function needs and a test cannot pass a different one — "
    "whoever populates this context owns what it carries and when it is reset"
)
_FOREIGN_WHY = (
    "writing another module's attribute changes its state from outside, so its owner cannot see "
    "who changes it or keep its invariants — whoever writes there owns those invariants from the "
    "outside"
)
_CLASS_WHY = (
    "a container on the class is shared by every instance, so instances and tests leak into each "
    "other — whoever keeps it there owns splitting per-instance state from class state"
)
_IMPORT_WHY = (
    "the effect belongs to importing the module, so import order and the host decide what the "
    "process does and no caller can import it without that happening — whoever adds one owns "
    "moving it to the caller or the composition root"
)
_BACKGROUND_WHY = (
    "nothing retains that work, so its failure is never observed and a restart loses it without a "
    "trace — whoever starts it owns its registry, its errors and its shutdown"
)
_BUNDLE_WHY = (
    "a root's bundle named outside its root fixes the object graph in more than one place, so the "
    "composition the root builds is no longer the one place that decides it — whoever names it "
    "here owns keeping the root the only place the graph is fixed"
)


# meiva:note
# One rule's report: why the rule exists, and the repair a site that hits it owes. The message
# prints both, so a new rule cannot be added without saying why it exists.
@dataclass(frozen=True)
class _Rule:
    why: str
    fix: str


_RULES: dict[str, _Rule] = {
    scan.INSTANCE: _Rule(_STATE_WHY, f"module-level instance — {_STATE_FIX}"),
    scan.CONTAINER: _Rule(_STATE_WHY, f"module-level mutable container — {_STATE_FIX}"),
    scan.CONTEXTVAR: _Rule(
        _CONTEXT_WHY,
        "pass the caller's runtime context explicitly as an argument instead of "
        "carrying dependencies through an ambient ContextVar",
    ),
    scan.GLOBAL_REBIND: _Rule(_STATE_WHY, f"a function rebinds a module global — {_STATE_FIX}"),
    scan.FOREIGN: _Rule(
        _FOREIGN_WHY,
        "rebinds an attribute of another module — give the owner a setter or inject the value",
    ),
    scan.CLASS_CONTAINER: _Rule(
        _CLASS_WHY,
        "mutable container shared through a class attribute — make it per-instance state",
    ),
    scan.SINGLETON: _Rule(
        _STATE_WHY,
        f"a zero-argument cache is a hidden singleton — {_STATE_FIX}; a memoized pure derivation is listed under [ambient-state.sites] with a reason",
    ),
    scan.CACHE: _Rule(
        _STATE_WHY,
        "a memoized function is state unless it is pure — a pure derivation is listed under [ambient-state.sites] in meizel.policy.toml with a reason",
    ),
    scan.CALL: _Rule(
        _IMPORT_WHY,
        "a call that runs at import (a registry fill or side effect) — register from a composition root, not at import",
    ),
    bundle.BUNDLE_LEAK: _Rule(
        _BUNDLE_WHY, f"a root bundle named outside its composition root — {bundle.FIX}"
    ),
    scan.READ: _Rule(
        _IMPORT_WHY,
        "reads settings, the environment, the clock or the filesystem at import — read it where it is used, or inject it",
    ),
    scan.HOST: _Rule(
        _IMPORT_WHY,
        "a platform constant computed at import — inject a `Platform` instead of recomputing the fact per module",
    ),
    scan.TASK: _Rule(_BACKGROUND_WHY, f"a free-floating task — {_BACKGROUND_FIX}"),
    scan.THREAD: _Rule(_BACKGROUND_WHY, f"a free-floating thread — {_BACKGROUND_FIX}"),
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
        (
            1,
            f"stale ambient_state list entry {key} — the site is gone; remove it from {_LIST_FILE}. {_STALE_WHY}",
        )
        for key in sorted(listed)
        if key not in present
    ]
    if rel in allow.SINK_FACADES and not any(_is_facade_state(rel, hit) for hit in hits):
        errors.append(
            (
                1,
                f"stale [ambient-state.sink-facades] entry — the file holds no module-level state; remove it from {_LIST_FILE}. {_STALE_WHY}",
            )
        )
    return errors


_LIST_FILE = allowances.POLICY
# meiva:note
# Why a closed list may not keep an entry nothing reports: it is the only place the reader learns
# that a site was let through, so an entry that outlives its site teaches the opposite.
_STALE_WHY = "an exemption that outlives its site is a permission nobody re-checks"


def missing_allowlist_errors(index: Tree) -> list[str]:
    """A listed file or function that no longer exists is stale too."""
    listed_paths = {
        *allow.SINK_FACADES,
        *(key.partition("::")[0] for key in allow.ALLOWED),
    }
    errors = [
        f"{path}:1: stale ambient_state list entry — the file no longer exists; remove it from {_LIST_FILE}. {_STALE_WHY}"
        for path in sorted(listed_paths)
        if not index.has_file(path)
    ]
    for callee in sorted(allow.PURE_REPO_CALLEES):
        owner, _, name = callee.rpartition(".")
        rel = index.module_path(owner)
        source = None if rel is None else index.read(rel)
        if source is None or f"def {name}(" not in source:
            errors.append(
                f"{_LIST_FILE}:1: stale [ambient-state.pure-repo-callees] entry {callee} — no such function; remove it. {_STALE_WHY}"
            )
    return errors


# meiva:note
# The failure text for a new or grown site, or None when `target` is not a known `rule:name`: what
# the site holds, the repair, why the rule exists, what the site's author now owns, and where the
# rule is written.
def site_message(target: str) -> str | None:
    rule, separator, name = target.partition(":")
    if rule not in _RULES or not (separator and name):
        return None
    return f"`{rule}` `{name}`: {_RULES[rule].fix}; {_RULES[rule].why}. See {_DOC}."


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
