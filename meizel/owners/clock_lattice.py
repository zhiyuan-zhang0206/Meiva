from __future__ import annotations

import ast
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass

from meizel import allowances

__all__ = [
    "Lattice",
    "literal_bindings",
    "registered_clocks",
    "scan_source",
    "stale_allowlist_entries",
]


# meiva:note
# The clock lattice this repository declares, as one value: the family modules that may define
# lattice vocabulary, the constants a policy exempts (`path::NAME -> why`), and the clocks those
# family modules register in their `CLOCKS` tables. A caller reads the policy and the family module
# sources once and hands this down; the rule itself opens nothing.
@dataclass(frozen=True, slots=True)
class Lattice:
    family_modules: tuple[str, ...]
    independent: Mapping[tuple[str, str], str]
    registered: frozenset[str]

    @classmethod
    def of(cls, policy: allowances.Policy, sources: Mapping[str, str]) -> Lattice:
        family_modules = tuple(policy.names("clock-lattice", "family-modules"))
        independent = {
            (path, name): why
            for site, why in policy.reasons("clock-lattice", "independent-clocks").items()
            for path, _, name in [site.partition("::")]
        }
        return cls(family_modules, independent, registered_clocks(family_modules, sources))


# meiva:note
# The clocks the family modules' `CLOCKS` tables name, read from the sources the caller supplies.
def registered_clocks(
    family_modules: tuple[str, ...], sources: Mapping[str, str]
) -> frozenset[str]:
    names: set[str] = set()
    for rel in family_modules:
        source = sources.get(rel)
        if source is None:
            continue
        for node in ast.parse(source).body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            if (
                isinstance(target, ast.Name)
                and target.id == "CLOCKS"
                and isinstance(node.value, ast.Dict)
            ):
                for key in node.value.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        names.add(key.value)
    return frozenset(names)


# meiva:note
# Lattice vocabulary: a module-level constant carrying any of these substrings is presumed to be
# part of the clock lattice until proven otherwise.
_LATTICE_TERMS = (
    "STALL",
    "GRACE",
    "REAP",
    "BUDGET",
    "WEDGED",
    "NO_PROGRESS",
    "LAUNCH_CONFIRM",
    "LEASE_TTL",
    "LEASE_RENEW",
    "SCAN_INTERVAL",
    "REAP_INTERVAL",
)

_CONST_NAME = re.compile(r"^_?[A-Z][A-Z0-9_]*$")


def _is_unregistered_clock(rel: str, name: str, value: ast.expr | None, lattice: Lattice) -> bool:
    """A lattice-vocabulary constant outside the family modules, not exempt and not an alias."""
    if not _CONST_NAME.match(name) or not any(term in name for term in _LATTICE_TERMS):
        return False
    if rel in lattice.family_modules:
        return False  # rule 1: the lattice's homes
    if (rel, name) in lattice.independent:
        return False  # rule 3: explicit exemption
    # rule 2: alias of a registered clock
    return not (isinstance(value, ast.Name) and value.id in lattice.registered)


def _carries_vocabulary(name: str) -> bool:
    """Lattice vocabulary as whole snake_case words (so `install` does not carry `stall`)."""
    padded = f"_{name.lower()}_"
    return any(f"_{term.lower()}_" in padded for term in _LATTICE_TERMS)


def _literal(node: ast.expr | None) -> bool:
    """A number, or an expression made only of numbers."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (int, float)) and not isinstance(node.value, bool)
    if isinstance(node, ast.UnaryOp):
        return isinstance(node.op, (ast.USub, ast.UAdd)) and _literal(node.operand)
    if isinstance(node, ast.BinOp):
        return _literal(node.left) and _literal(node.right)
    return False


def _bound_names(target: ast.expr) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Attribute):
        return [target.attr]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for element in target.elts for name in _bound_names(element)]
    return []


def _assignment_bindings(
    node: ast.Assign | ast.AnnAssign, *, module_level: bool
) -> list[tuple[str, int]]:
    """The vocabulary-carrying names one assignment binds to a literal, minus module constants."""
    if not _literal(node.value):
        return []
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return [
        (name, node.lineno)
        for target in targets
        for name in _bound_names(target)
        if _carries_vocabulary(name) and not (module_level and _CONST_NAME.match(name))
    ]


def _keyword_bindings(call: ast.Call) -> list[tuple[str, int]]:
    return [
        (keyword.arg, keyword.value.lineno)
        for keyword in call.keywords
        if keyword.arg and _literal(keyword.value) and _carries_vocabulary(keyword.arg)
    ]


def literal_bindings(tree: ast.Module) -> list[tuple[str, int]]:
    """(name, line) of every vocabulary-carrying name bound to a literal, outside module constants.

    The bindings are field defaults, assignments in any scope and call keyword arguments; a
    module-level `UPPER_CASE` constant is left to the constant rules above.
    """
    found: list[tuple[str, int]] = []
    module_level = {id(node) for node in tree.body}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            found.extend(_assignment_bindings(node, module_level=id(node) in module_level))
        elif isinstance(node, ast.Call):
            found.extend(_keyword_bindings(node))
    return found


def _module_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names


# meiva:note
# The findings for one file, from its repository-relative name and its text. A file that does not
# parse is left to the compiler; a family module may define the vocabulary, everything else may
# only alias a registered clock or name an exempted constant.
def scan_source(rel: str, source: str, *, lattice: Lattice) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    errors: list[str] = []
    if rel not in lattice.family_modules:
        errors.extend(
            f"{rel}:{line}: {name} — a lattice-vocabulary name bound to a literal outside the "
            "lattice family modules; give it no value here (take it from a config the root builds) "
            "or define the clock in a family module and register it in CLOCKS"
            for name, line in literal_bindings(tree)
            if (rel, name) not in lattice.independent
        )
    errors.extend(_constant_errors(rel, tree, lattice=lattice))
    return errors


# meiva:note
# The findings for one module-level constant outside the family modules.
def _constant_errors(rel: str, tree: ast.Module, *, lattice: Lattice) -> list[str]:
    registry = lattice.family_modules[0] if lattice.family_modules else "a lattice family module"
    errors: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and _is_unregistered_clock(
                rel, target.id, node.value, lattice
            ):
                errors.append(
                    f"{rel}:{node.lineno}: {target.id} — lattice-vocabulary clock outside "
                    f"the lattice family modules; define it in {registry} (and "
                    "register it in CLOCKS) or make it an alias of a registered clock"
                )
    return errors


# meiva:note
# Allowlist entries whose file or exempted constant is gone. `existing` is the checkout's
# repository-relative file set and `sources` the text of the exempt files the caller could read:
# a stale entry that outlives its reason is a permission nobody re-checks.
def stale_allowlist_entries(
    existing: Collection[str], sources: Mapping[str, str], *, lattice: Lattice
) -> list[str]:
    errors: list[str] = []
    for rel in lattice.family_modules:
        if rel not in existing:
            errors.append(f"{rel}: stale FAMILY_MODULES entry — the file no longer exists")
    for rel, name in lattice.independent:
        source = sources.get(rel)
        if source is None:
            errors.append(
                f"{rel}: stale INDEPENDENT_CLOCKS entry {name} — the file no longer exists"
            )
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue  # unreadable or a syntax error the compiler owns: cannot judge
        if name.islower():
            if name not in {bound for bound, _ in literal_bindings(tree)}:
                errors.append(
                    f"{rel}: stale INDEPENDENT_CLOCKS entry {name} — nothing binds {name} to a "
                    "literal there any more; drop the exemption"
                )
        elif name not in _module_level_names(tree):
            errors.append(
                f"{rel}: stale INDEPENDENT_CLOCKS entry {name} — no module-level {name} "
                "is defined there any more; drop the exemption"
            )
    return errors


if __name__ == "__main__":
    raise SystemExit(
        "the entry of this rule is `python -m meizel.owners.clock_lattice_cli`: the rule takes the "
        "policy, the checkout's file set and the files' text from the shell that reads them"
    )
