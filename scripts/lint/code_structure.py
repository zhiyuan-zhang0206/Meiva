# meiva:link docs/conventions/python-conventions.md
# meiva:link scripts/lint/docs/lint.meiva.okf.md
# meiva:link meizel/docs/ambient-state.meiva.okf.md

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import partial
from pathlib import Path, PurePosixPath

from radon.complexity import ComplexityVisitor

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

sys.path.insert(0, str(_REPO_ROOT))

from meizel import allowances, lint_common  # noqa: E402
from meizel.budgets import limits  # noqa: E402
from meizel.checkout import load_universe, roles_at  # noqa: E402
from meizel.contracts.analysis import (  # noqa: E402
    load_manifests,
    load_policy,
    type_checking_in_scope,
    type_checking_violations,
)
from meizel.graph.model import MeizelError, Sources  # noqa: E402
from meizel.graph.universe import Universe  # noqa: E402
from meizel.language import path_imports  # noqa: E402
from meizel.language.scan import scan_tree  # noqa: E402
from meizel.owners import decisions  # noqa: E402
from meizel.roles import select_packages  # noqa: E402
from meizel.state import ambient_state, machine_role  # noqa: E402
from scripts.lint.diagnostics import no_ambient_fallback  # noqa: E402
from scripts.structure import baseline_shards, locality  # noqa: E402
from scripts.structure.budgets import quality_budget  # noqa: E402

ROLES = roles_at(_REPO_ROOT)

__all__ = ["check_shallow_split", "is_shallow_split_filename", "main"]

_SHALLOW_SPLIT_PATTERN = re.compile(r"_part[0-9]+\.py$")
_SHALLOW_SPLIT_GUIDANCE = (
    "mechanical shallow-split filename '{name}' is forbidden — avoid mechanical "
    "_part[0-9].py or naked *_helpers.py splits. Instead, use one of 3 clean "
    "refactoring shapes: (1) Extract pure data contracts to contracts.py, "
    "(2) Extract strict Pure Core module, (3) Create a cohesive subpackage "
    "with a narrow meizel.toml exports door."
)


# meiva:note
# Whether filename matches *_part[0-9].py or naked helpers.py / _helpers.py.
def is_shallow_split_filename(filename: str) -> bool:
    if filename in {"helpers.py", "_helpers.py"}:
        return True
    return bool(_SHALLOW_SPLIT_PATTERN.search(filename))


# meiva:note
# Check whether a non-test file is a mechanical shallow split.
def check_shallow_split(rel_path: str) -> str | None:
    if lint_common.is_test_path(rel_path):
        return None
    name = PurePosixPath(rel_path).name
    if is_shallow_split_filename(name):
        return f"{rel_path}:1: {_SHALLOW_SPLIT_GUIDANCE.format(name=name)}"
    return None


# Baseline sections whose frozen `path::target` site counts must match reality exactly.
_SITE_SECTIONS = (*locality.EXTERNAL_SECTIONS, ambient_state.SECTION)
_MEASURED_SECTIONS = (*locality.SECTIONS, path_imports.SECTION, ambient_state.SECTION)

# AST rules track the application packages; budgets also cover tooling/tests.
_SCAN_DIRS = lint_common.FRAMEWORK_DIRS
_AST_DIRS = select_packages(
    ROLES, "application", "standalone", "templates"
)  # _SCAN_DIRS plus schedules/, which only Rule 9 governs
_STRUCTURE_DIRS = select_packages(ROLES, "application", "standalone", "tests", "tooling")


# meiva:note
# Whether each exempted module still exists, asked once here: the rule answers from a value.
def existing_files(repo_root: Path, table: decisions.Table) -> set[str]:
    paths = {path for decision in table.values() for path in decision.allowed}
    return {path for path in paths if (repo_root / path).is_file()}


def _scan_file(tree: ast.Module, rel_path: str, table: decisions.Table) -> list[tuple[int, str]]:
    """Return AST violations as [(lineno, message), ...]."""
    if lint_common.is_test_path(rel_path):
        return []  # tests are outside the AST rules (module docstring)
    out: list[tuple[int, str]] = []
    out.extend(machine_role.violations(tree))
    out.extend(decisions.allowlist_errors(tree, rel_path, _SCAN_DIRS, table))
    out.extend(no_ambient_fallback.violations(tree, rel_path))
    return out


# meiva:note
# Collect errors for any non-test files using forbidden shallow-split filename conventions.
def _shallow_split_errors(paths: Iterable[Path], repo_root: Path) -> list[str]:
    errors: list[str] = []
    for path in sorted(paths):
        try:
            rel = path.relative_to(repo_root).as_posix()
        except ValueError:
            continue
        err = check_shallow_split(rel)
        if err is not None:
            errors.append(err)
    return errors


def _in_scan_scope(rel_path: str) -> bool:
    return any(rel_path == d or rel_path.startswith(f"{d}/") for d in _SCAN_DIRS)


def _iter_py_files(roots: list[Path], repo_root: Path) -> list[Path]:
    return lint_common.python_files(roots, repo_root)


def _parse_baseline(
    shards: dict[str, str], *, renames: dict[str, str] | None = None, table: decisions.Table
) -> dict[str, dict[str, int]]:
    """Validate the remaining site exemptions; any other section is an error."""
    baseline = baseline_shards.merge(shards, _SITE_SECTIONS)
    for kind in _SITE_SECTIONS:
        scope = _AST_DIRS if kind == ambient_state.SECTION else _SCAN_DIRS
        locality.validate_entries(kind, baseline[kind], _carried_scope(scope, renames or {}), table)
    return baseline


def _carried_scope(scope: tuple[str, ...], renames: dict[str, str]) -> tuple[str, ...]:
    """`scope` plus every top-level directory a detected rename moves files into it from."""
    carried = {
        PurePosixPath(old).parts[0] for old, new in renames.items() if new.split("/")[0] in scope
    }
    return tuple(sorted(set(scope) | carried))


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — local git query, no shell
        ["git", "-C", str(repo_root), *args], capture_output=True, text=True, check=False
    )


def _baseline_base(
    repo_root: Path,
    explicit: str | None,
    *,
    env_base: str | None = None,
    use_env_base: bool = True,
) -> str:
    """Resolve an explicit snapshot or the actual merge base with origin/main."""
    env_ref = (
        env_base
        if env_base is not None
        else (os.environ.get("LINT_STRUCTURE_BASELINE_BASE") if use_env_base else None)
    )
    ref = explicit if explicit is not None else env_ref
    if ref is not None:
        result = _git(repo_root, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
        if result.returncode:
            source = "baseline_base" if explicit is not None else "LINT_STRUCTURE_BASELINE_BASE"
            raise ValueError(f"{source}={ref!r} cannot resolve to a commit")
    else:
        result = _git(repo_root, "merge-base", "--", "HEAD", "origin/main")
        result.check_returncode()
    return result.stdout.strip()


def _rename_map(repo_root: Path, base: str) -> dict[str, str]:
    """Old -> new paths for the renames git -M detects between `base` and the working tree.

    A detected rename carries remaining frozen site keys to the new path: move the
    baseline entries with it — remove the old key, add the new one with the same
    value — and the guard accepts the edit. The frozen values still cap the new
    path: a raise stays a violation and a new key without a paired removal stays
    an addition. A rewrite git no longer detects as a rename is evaluated fresh.
    With -B, a module whose old path now holds a total rewrite still counts as
    moved (git reports that pair as a copy, `C`, of the rewritten source).
    `-l0` lifts `diff.renameLimit`: a package-wide move edits thousands of files at
    once, and past the limit git silently stops pairing edited moves.
    """
    result = _git(
        repo_root,
        "diff",
        "-M",
        "-B",
        "-l0",
        "--name-status",
        "--diff-filter=RC",
        "--no-color",
        base,
    )
    result.check_returncode()
    renames: dict[str, str] = {}
    for line in result.stdout.splitlines():
        status, _, rest = line.partition("\t")
        old, separator, new = rest.partition("\t")
        if status.startswith(("R", "C")) and separator and old and new:
            renames[old] = new
    return renames


def _remap_renamed_keys(entries: dict[str, int], renames: dict[str, str]) -> dict[str, int]:
    """Carry remaining site keys when Git detects a file move."""
    remapped: dict[str, int] = {}
    for name, value in entries.items():
        path, separator, target_name = name.partition("::")
        new_path = renames.get(path, path)
        if new_path != path:
            old_top = path.split("/", 1)[0]
            new_top = new_path.split("/", 1)[0]
            if old_top != new_top:
                target_name = target_name.replace(f":{old_top}.", f":{new_top}.")
        target = f"{new_path}{separator}{target_name}"
        remapped[target] = min(remapped.get(target, value), value)
    return remapped


def _renamed_to(name: str, renames: dict[str, str]) -> str | None:
    return renames.get(name.partition("::")[0])


# meiva:note
# Why the guard refuses a moved key that stayed behind, and a rule version that went back: the
# frozen count is a fact about one file, and a version records the measurement the counts were
# frozen under. Both are written once here, with the guard that reads them.
_RENAME_WHY = (
    "the frozen count belongs to the file, so a key left behind freezes a path nothing measures "
    "while the sites at the new path go unguarded — whoever moves the file owns carrying it"
)
_VERSION_WHY = (
    "a version records the measurement its counts were frozen under, so going back reads an older "
    "count with a rule that no longer produces it — a measurement change raises the version and "
    "keeps every count frozen"
)


def _section_guard(
    kind: str,
    current: dict[str, int],
    previous: dict[str, int],
    *,
    renames: dict[str, str] | None = None,
) -> list[str]:
    errors: list[str] = []
    additions = current.keys() - previous.keys()
    for name in sorted(additions):
        moved_to = _renamed_to(name, renames or {})
        if moved_to is not None:
            new_key = moved_to + name[len(name.partition("::")[0]) :]
            errors.append(
                f"{baseline_shards.shard_path(kind, name)}: {kind} entry {name} was not migrated "
                f"after its file moved to {moved_to} — move this key to {new_key} in "
                f"{baseline_shards.shard_path(kind, new_key)} with the same value; {_RENAME_WHY}"
            )
            continue
        errors.append(
            f"{baseline_shards.shard_path(kind, name)}: added {kind} entry {name} — baseline is "
            f"shrink-only. {baseline_shards.SHRINK_ONLY}"
        )
    for name in sorted(current.keys() & previous.keys()):
        if current[name] > previous[name]:
            errors.append(
                f"{baseline_shards.shard_path(kind, name)}: raised {kind} entry {name} from {previous[name]} "
                f"to {current[name]} — baseline is shrink-only. {baseline_shards.SHRINK_ONLY}"
            )
    return errors


def _baseline_guard(
    baseline: dict[str, dict[str, int]],
    *,
    repo_root: Path,
    base: str,
    table: decisions.Table,
    renames: dict[str, str] | None = None,
) -> list[str]:
    try:
        shards = baseline_shards.read_at(repo_root, base)
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        return [f"{baseline_shards.SHARD_DIR}: {exc}"]
    try:
        previous = _parse_baseline(shards, renames=renames, table=table)
    except ValueError as exc:
        return [f"{baseline_shards.SHARD_DIR}: invalid base baseline ({base}): {exc}"]
    try:
        rules_was, rules_now = baseline_shards.read_rules(repo_root, base)
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        return [f"{baseline_shards.SHARD_DIR}: invalid rule versions: {exc}"]
    errors: list[str] = []
    for kind, entries in baseline.items():
        was, now = rules_was.get(kind, 1), rules_now.get(kind, 1)
        if now < was:
            errors.append(
                f"{baseline_shards.SHARD_DIR}/{baseline_shards.RULES_FILE}: "
                f"{kind} rule version went back from {was} to {now}; {_VERSION_WHY}"
            )
        errors.extend(
            _section_guard(
                kind,
                entries,
                _remap_renamed_keys(previous[kind], renames or {}),
                renames=renames,
            )
        )
    return errors


def _ast_rule_files(argv: list[str], repo_root: Path) -> set[Path]:
    # Preserve the AST rules' original resolved-target scope, including aliases.
    targets = [Path(a).resolve() for a in argv] if argv else [repo_root / d for d in _AST_DIRS]
    return set(_iter_py_files(targets, repo_root))


def _collect_locality(
    tree: ast.Module,
    rel: str,
    sites: dict[str, locality.Sites],
    scanned: set[str],
    scopes: path_imports.Scopes,
    table: decisions.Table,
) -> None:
    scanned.add(rel)
    for kind, found in locality.measure(tree, rel, _SCAN_DIRS, table).items():
        sites[kind].update(found)
    sites[path_imports.SECTION].update(path_imports.measure(tree, rel, scopes))


@dataclass(frozen=True)
class _TypeContext:
    universe: Universe
    sources: Sources
    policy: allowances.Policy
    allowed: frozenset[str]
    packages: tuple[str, ...]
    files: set[Path]
    scopes: path_imports.Scopes

    def in_scope(self, path: Path, rel: str) -> bool:
        return path in self.files and type_checking_in_scope(rel, self.packages)

    def errors(self, rel: str, tree: ast.Module) -> list[str]:
        walrus = any(isinstance(n, ast.NamedExpr) for n in ast.walk(tree))
        scan = scan_tree(self.universe, rel, tree, self.sources, walrus=walrus, policy=self.policy)
        return [
            f"{rel}:{line}: {message}"
            for line, message in type_checking_violations(
                scan.type_checking, rel, self.allowed, self.packages
            )
        ]


def _load_type_context(argv: list[str], repo_root: Path) -> tuple[_TypeContext | None, list[str]]:
    universe = load_universe(repo_root)
    manifests, manifest_problems = load_manifests(universe)
    policy, policy_problems = load_policy(universe)
    if manifest_problems or policy_problems:
        return None, [
            f"{d.path}:{d.line}: {d.message}" for d in [*manifest_problems, *policy_problems]
        ]
    root_manifest = manifests.get("")
    type_packages = select_packages(
        root_manifest.roles if root_manifest else {},
        "application",
        "standalone",
        "tooling",
        "templates",
        "tests",
    )
    roots = (
        [Path(arg).resolve() for arg in argv]
        if argv
        else [repo_root / d for d in type_packages] + [repo_root / "conftest.py"]
    )
    return (
        _TypeContext(
            universe,
            Sources(),
            policy,
            frozenset(policy.reasons("type-checking", "files")),
            type_packages,
            set(_iter_py_files(roots, repo_root)),
            path_imports.Scopes.of(policy),
        ),
        [],
    )


def _governed(
    path: Path, rel: str, ast_files: set[Path], type_ctx: _TypeContext
) -> tuple[bool, bool, bool]:
    """(Rules 1, 3-6; Rule 9) govern this file: Rule 9 also reaches schedules/."""
    in_ast = path in ast_files
    return (
        in_ast and _in_scan_scope(rel),
        in_ast and ambient_state.in_scope(rel, _AST_DIRS),
        type_ctx.in_scope(path, rel),
    )


# meiva:note
# What one run admits: the tracked snapshot plus every file it reads off the working tree. A file
# written but not yet committed is present here, as it was for the disk probe this replaced.
def _admitted_files(universe: Universe, repo_root: Path, paths: set[Path]) -> set[str]:
    admitted = set(universe.files)
    for path in paths:
        if path.is_relative_to(repo_root):
            admitted.add(path.relative_to(repo_root).as_posix())
    return admitted


def _check_ast_and_quality(
    argv: list[str],
    targets: list[Path],
    baseline: dict[str, dict[str, int]],
    *,
    repo_root: Path,
    table: decisions.Table,
    renames: dict[str, str] | None = None,
    parse_ast: Callable[..., ast.Module],
    measure_quality: Callable[[ast.AST, str], dict[str, dict[str, int]]],
) -> list[str]:
    type_ctx, type_problems = _load_type_context(argv, repo_root)
    if type_ctx is None:
        return type_problems
    files = limits.budget_files(targets, repo_root, _STRUCTURE_DIRS)
    ast_files = _ast_rule_files(argv, repo_root)
    ambient_tree = ambient_state.Tree(
        _admitted_files(type_ctx.universe, repo_root, files | ast_files | type_ctx.files),
        type_ctx.universe.repo.load_text,
        type_ctx.universe.dirs,
    )
    measurements: dict[str, dict[str, int]] = {kind: {} for kind in quality_budget.QUALITY_SECTIONS}
    sites: dict[str, locality.Sites] = {kind: {} for kind in _MEASURED_SECTIONS}
    scanned: set[str] = set()
    errors: list[str] = []
    errors.extend(_shallow_split_errors(files | ast_files | type_ctx.files, repo_root))
    for path in sorted(files | ast_files | type_ctx.files):
        try:
            rel = path.relative_to(repo_root).as_posix()
        except ValueError:
            continue
        ast_rules, ambient, type_rules = _governed(path, rel, ast_files, type_ctx)
        if not any((path in files, ast_rules, ambient, type_rules)):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        tree = parse_ast(text, filename=rel)
        if type_rules:
            errors.extend(type_ctx.errors(rel, tree))
        if ast_rules:
            errors.extend(
                f"{rel}:{line}: {message}" for line, message in _scan_file(tree, rel, table)
            )
            _collect_locality(tree, rel, sites, scanned, type_ctx.scopes, table)
        if ambient:
            errors.extend(ambient_state.collect(tree, rel, ambient_tree, sites, scanned, _AST_DIRS))
        if path in files:
            for kind, values in measure_quality(tree, rel).items():
                measurements[kind].update(values)
    errors.extend(quality_budget.quality_errors(measurements))
    errors.extend(
        locality.site_errors(
            sites, baseline, scanned=scanned, repo_root=repo_root, table=table, renames=renames
        )
    )
    errors.extend(decisions.missing_allowlist_errors(existing_files(repo_root, table), table))
    errors.extend(ambient_state.missing_allowlist_errors(ambient_tree))
    return errors


def _changed_targets(only: list[str] | None, repo_root: Path) -> list[str]:
    """The `--only` changed files as explicit targets; none (the full scan) when the tooling changed."""
    scope = lint_common.changed_scope(only, repo_root)
    return [] if scope is None else [str(repo_root / rel) for rel in sorted(scope)]


def _parse_args(argv: list[str], repo_root: Path) -> tuple[list[str], bool]:
    argv, only = lint_common.split_only(argv)
    targets = [*argv, *_changed_targets(only, repo_root)]
    return targets, only == [] and not targets


def _budget_scopes(argv: list[str], repo_root: Path) -> tuple[list[Path], list[Path]]:
    """Explicit paths share one scope; a full run counts directories across the repo."""
    if argv:
        # Keep tracked symlinks lexical: count their names without following targets.
        selected = [Path(os.path.abspath(a)) for a in argv]  # noqa: PTH100 — normalize without following symlinks
        return selected, selected
    return [repo_root / directory for directory in _STRUCTURE_DIRS], [repo_root]


def main(
    argv: list[str] | None = None,
    *,
    repo_root: Path | None = None,
    baseline_base: str | None = None,
    env_base: str | None = None,
    use_env_base: bool = True,
    parse_ast: Callable[..., ast.Module],
    measure_quality: Callable[[ast.AST, str], dict[str, dict[str, int]]] | None = None,
) -> int:
    """Run the gate for CLI arguments against an explicit checkout and comparison commit.

    Omitted arguments use sys.argv; target paths retain cwd-relative CLI semantics.
    The comparison defaults to LINT_STRUCTURE_BASELINE_BASE, then the merge base
    of HEAD and origin/main.
    Diagnostics use stdout/stderr and the return value is the CLI exit status.
    """
    repo_root = repo_root.resolve() if repo_root is not None else _REPO_ROOT
    if measure_quality is None:
        measure_quality = partial(
            quality_budget.measure_quality, visitor_from_ast=ComplexityVisitor.from_ast
        )
    argv, nothing_changed = _parse_args(argv if argv is not None else sys.argv[1:], repo_root)
    if nothing_changed:
        return 0
    missing = [arg for arg in argv if not os.path.lexists(arg)]
    if missing:
        print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
        return 1
    targets, directory_targets = _budget_scopes(argv, repo_root)
    # meiva:note
    # Rule 5's table, read once per run from the checkout being judged: the decisions a policy
    # declares and the exemptions it grants are values, so `meizel/owners/decisions.py` opens
    # nothing and a synthetic root is judged by its own policy.
    try:
        table = decisions.decision_table(allowances.load(repo_root))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"{allowances.POLICY}: invalid policy: {exc}", file=sys.stderr)
        return 1
    try:
        baseline = _parse_baseline(baseline_shards.read_worktree(repo_root), table=table)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"{baseline_shards.SHARD_DIR}: invalid baseline: {exc}", file=sys.stderr)
        return 1
    try:
        base = _baseline_base(
            repo_root,
            baseline_base,
            env_base=env_base,
            use_env_base=use_env_base,
        )
        renames = _rename_map(repo_root, base)
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"{baseline_shards.SHARD_DIR}: cannot establish baseline comparison: {exc}")
        return 1
    errors = _baseline_guard(baseline, repo_root=repo_root, base=base, table=table, renames=renames)
    try:
        errors.extend(limits.check(targets, directory_targets, repo_root, _STRUCTURE_DIRS))
    except (MeizelError, OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"cannot read tracked directory structure: {exc}", file=sys.stderr)
        return 1
    errors.extend(
        _check_ast_and_quality(
            argv,
            targets,
            baseline,
            repo_root=repo_root,
            table=table,
            renames=renames,
            parse_ast=parse_ast,
            measure_quality=measure_quality,
        )
    )
    for error in errors:
        print(error)
    if errors:
        print(
            f"\n{len(errors)} hard violation(s). These rules keep the import graph acyclic and "
            f"every shared fact owned once, so each violation is a boundary that will be crossed "
            f"again until the side it is on is fixed: move the code to the side the rule allows, "
            f"or declare the edge in the meizel.toml of the directory where the two sides meet. "
            f"Raising the baseline is not the repair. See scripts/lint/docs/lint.meiva.okf.md and "
            f"docs/conventions/architecture/code-placement-and-purity.md.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(parse_ast=ast.parse))
