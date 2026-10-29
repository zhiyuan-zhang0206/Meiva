from __future__ import annotations

import ast
import re
import sys
from collections.abc import Mapping
from pathlib import Path

from meizel import allowances, lint_common
from meizel.roles import packages

_REPO_ROOT = Path(__file__).resolve().parents[2]

SCAN_DIRS = packages("application", "standalone", "tooling")

_TEST_PATTERNS = (
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(r"_test\.py$"),
)

# Lattice vocabulary: a module-level constant carrying any of these substrings is
# presumed to be part of the clock lattice until proven otherwise.
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

_POLICY = allowances.load(_REPO_ROOT)

# The lattice family modules: lattice vocabulary may be DEFINED here (and only here). Listed in
# meizel.policy.toml under [clock-lattice].
FAMILY_MODULES = _POLICY.names("clock-lattice", "family-modules")

_CONST_NAME = re.compile(r"^_?[A-Z][A-Z0-9_]*$")


def _parse_registered_clocks() -> frozenset[str]:
    names: set[str] = set()
    for rel in FAMILY_MODULES:
        source = lint_common.read_utf8_text(_REPO_ROOT / rel)
        if source is None:
            continue
        tree = ast.parse(source)
        for node in tree.body:
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


REGISTERED_CLOCKS: frozenset[str] = _parse_registered_clocks()


def _registered_clock_names() -> frozenset[str]:
    return REGISTERED_CLOCKS


# Explicit exemptions: `(relative_path, NAME)` -> why this lattice-vocabulary constant may live
# outside the family modules. Either it is not a clock at all (a flag, a key, a collection), or it
# is a genuinely independent clock with no lattice neighbour. Every entry states which, in
# meizel.policy.toml under [clock-lattice.independent-clocks] (`path::NAME`).
INDEPENDENT_CLOCKS: dict[tuple[str, str], str] = {
    (path, name): why
    for site, why in _POLICY.reasons("clock-lattice", "independent-clocks").items()
    for path, _, name in [site.partition("::")]
}


def _is_unregistered_clock(
    rel: str,
    name: str,
    value: ast.expr | None,
    *,
    family_modules: tuple[str, ...] = FAMILY_MODULES,
    independent: Mapping[tuple[str, str], str] = INDEPENDENT_CLOCKS,
    registered_clocks: frozenset[str] = REGISTERED_CLOCKS,
) -> bool:
    """A lattice-vocabulary constant outside the family modules, not exempt and not an alias."""
    if not _CONST_NAME.match(name) or not any(term in name for term in _LATTICE_TERMS):
        return False
    if rel in family_modules:
        return False  # rule 1: the lattice's homes
    if (rel, name) in independent:
        return False  # rule 3: explicit exemption
    # rule 2: alias of a registered clock
    return not (isinstance(value, ast.Name) and value.id in registered_clocks)


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


def _root(repo_root: Path | None) -> Path:
    return _REPO_ROOT if repo_root is None else repo_root


def _rel_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _constant_errors(
    path: Path,
    rel: str,
    tree: ast.Module,
    *,
    family_modules: tuple[str, ...],
    exempt: Mapping[tuple[str, str], str],
    registered_clocks: frozenset[str],
) -> list[str]:
    registry = family_modules[0] if family_modules else "a lattice family module"
    errors: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and _is_unregistered_clock(
                rel,
                target.id,
                node.value,
                family_modules=family_modules,
                independent=exempt,
                registered_clocks=registered_clocks,
            ):
                errors.append(
                    f"{path}:{node.lineno}: {target.id} — lattice-vocabulary clock outside "
                    f"the lattice family modules; define it in {registry} (and "
                    "register it in CLOCKS) or make it an alias of a registered clock"
                )
    return errors


def scan_file(
    path: Path,
    *,
    independent: Mapping[tuple[str, str], str] | None = None,
    repo_root: Path | None = None,
    family_modules: tuple[str, ...] = FAMILY_MODULES,
    registered_clocks: frozenset[str] = REGISTERED_CLOCKS,
) -> list[str]:
    rel = _rel_path(path, _root(repo_root))
    source = lint_common.read_utf8_text(path)
    if source is None:
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []  # unreadable entry (dangling symlink / non-UTF-8), or a syntax error the compiler owns
    errors: list[str] = []
    exempt = INDEPENDENT_CLOCKS if independent is None else independent
    if rel not in family_modules:
        errors.extend(
            f"{path}:{line}: {name} — a lattice-vocabulary name bound to a literal outside the "
            "lattice family modules; give it no value here (take it from a config the root builds) "
            "or define the clock in a family module and register it in CLOCKS"
            for name, line in literal_bindings(tree)
            if (rel, name) not in exempt
        )
    errors.extend(
        _constant_errors(
            path,
            rel,
            tree,
            family_modules=family_modules,
            exempt=exempt,
            registered_clocks=registered_clocks,
        )
    )
    return errors


def _module_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names


def stale_allowlist_entries(
    independent: Mapping[tuple[str, str], str] | None = None,
    *,
    repo_root: Path | None = None,
    family_modules: tuple[str, ...] = FAMILY_MODULES,
) -> list[str]:
    """Allowlist entries whose file or exempted constant no longer exists."""
    root = _REPO_ROOT if repo_root is None else repo_root
    exempt = INDEPENDENT_CLOCKS if independent is None else independent
    errors: list[str] = []
    for rel in family_modules:
        if not (root / rel).is_file():
            errors.append(f"{rel}: stale FAMILY_MODULES entry — the file no longer exists")
    for rel, name in exempt:
        source = lint_common.read_utf8_text(root / rel)
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


def _scan(
    paths: list[Path],
    scope: frozenset[str] | None = None,
    *,
    repo_root: Path | None = None,
    registered_clocks: frozenset[str] = REGISTERED_CLOCKS,
) -> list[str]:
    root = _root(repo_root)
    errors: list[str] = []
    for path in paths:
        candidates = lint_common.python_files([path], root)
        for p in candidates:
            rel = _rel_path(p, root)
            if any(pat.search(rel) for pat in _TEST_PATTERNS):
                continue
            if scope is not None and rel not in scope:
                continue
            errors.extend(scan_file(p, repo_root=root, registered_clocks=registered_clocks))
    return errors


def _stale_entries(
    repo_root: Path | None,
    family_modules: tuple[str, ...],
    independent: Mapping[tuple[str, str], str] | None,
) -> list[str]:
    if repo_root is None and family_modules == FAMILY_MODULES and independent is None:
        return stale_allowlist_entries()
    return stale_allowlist_entries(
        independent, repo_root=_root(repo_root), family_modules=family_modules
    )


def main(
    argv: list[str] | None = None,
    *,
    repo_root: Path | None = None,
    registered_clocks: frozenset[str] = REGISTERED_CLOCKS,
    family_modules: tuple[str, ...] = FAMILY_MODULES,
    independent: Mapping[tuple[str, str], str] | None = None,
) -> int:
    root = _root(repo_root)
    argv = list(sys.argv[1:] if argv is None else argv)
    argv, only = lint_common.split_only(argv)
    if argv:
        resolved, missing = lint_common.resolve_targets(argv, root)
        if missing:
            print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
            return 1
        paths = [root / entry for entry in resolved if entry.endswith(".py")]
    else:
        paths = lint_common.scan_roots(root, SCAN_DIRS)
    # `--only` (the commit hook) judges the changed files under the default scope; the
    # lattice's own family modules are inputs: editing one re-judges every file.
    scope = lint_common.changed_scope(only, root, inputs=family_modules)
    errors = _scan(paths, scope, repo_root=root, registered_clocks=registered_clocks)
    if not argv:
        errors.extend(_stale_entries(repo_root, family_modules, independent))
    for err in errors:
        print(err, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
