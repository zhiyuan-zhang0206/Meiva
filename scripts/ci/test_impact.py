"""Runtime dependency closure for backend test selection, without executing code."""

from __future__ import annotations

import ast
import io
import os
import subprocess
import tarfile
import tempfile
import tomllib
from collections import deque
from collections.abc import Generator, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol, cast

from meizel.allowances import POLICY, Policy, parse
from meizel.contracts.sync import prepare_policy_snapshot
from scripts.structure.imports.facts import UNANCHORED_READ, Fact, FactKind, Unknown, collect
from scripts.structure.imports.fixture_scopes import declarations
from scripts.structure.placement import CODE_TOPS, ModuleIndex

_TOPS = tuple(dict.fromkeys((*CODE_TOPS, "tests")))
_SKIPPED = frozenset({"node_modules", "__pycache__"})
_MODULE_FACTS = frozenset(
    {
        FactKind.IMPORT,
        FactKind.DYNAMIC_IMPORT,
        FactKind.EMBEDDED_IMPORT,
        FactKind.PYTHON_MODULE,
    }
)


@dataclass(frozen=True)
class Impact:
    """Unpruned facts projected onto collectable tests, plus incomplete evidence.

    ``unknown`` holds the gaps no declaration covers: a file the policy lists under ``[loads]``
    or (outside test directories) ``[external]`` has declared its dynamic loads."""

    tests_by_input: dict[str, set[str]]
    unknown: tuple[Unknown, ...]
    members: dict[Fact, set[str]] = field(default_factory=dict)
    cutoff: frozenset[str] = frozenset()

    def runtime_tests(self, path: str) -> set[str]:
        # meiva:link docs/conventions/engineering/test-selection.md
        if path in self.cutoff:
            return set()
        tests = {
            test
            for parent in (Path(path), *Path(path).parents)
            for test in self.tests_by_input.get(parent.as_posix(), ())
        }
        tests.update(
            test
            for fact, consumers in self.members.items()
            if fact.matches_member(path)
            for test in consumers
        )
        return tests


def python_files(root: Path) -> Iterator[Path]:
    """Every .py file under the code tops, in stable order, plus the root conftest."""
    for top in _TOPS:
        directory = root / top
        if directory.is_dir():
            for current, dirs, files in os.walk(directory):
                dirs[:] = sorted(
                    part for part in dirs if not part.startswith(".") and part not in _SKIPPED
                )
                for name in sorted(files):
                    if name.endswith(".py") and not name.startswith("."):
                        yield Path(current) / name
    conftest = root / "conftest.py"
    if conftest.is_file():
        yield conftest


def code_names(file: Path, needle: str, *, exact: bool) -> bool:
    """Whether a Python file holds a string constant equal to (``exact``) or containing
    ``needle`` outside docstrings; a comment or a docstring reads nothing."""
    text = file.read_text(encoding="utf-8")
    if needle not in text:
        return False
    tree = ast.parse(text, filename=str(file))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and (node.value == needle if exact else needle in node.value)
        for node in ast.walk(tree)
    )


def module_files(module: str, index: ModuleIndex) -> set[str]:
    """Importing a module also executes its concrete package initializers."""
    parts = module.split(".")
    files = {
        file
        for end in range(1, len(parts))
        if (file := index.file(".".join(parts[:end]))) is not None and file.endswith("/__init__.py")
    }
    if (file := index.file(module)) is not None:
        files.add(file)
    return files


def _scope_tests(scope: str, tests: frozenset[str]) -> set[str]:
    return {test for test in tests if test == scope or test.startswith(f"{scope}/")}


def _fixture_edges(
    root: Path,
    tests: frozenset[str],
    index: ModuleIndex,
    incomplete: dict[str, tuple[Unknown, ...]],
) -> dict[str, set[str]]:
    """Each test depends on conftests, fixture modules and their declaration inputs."""
    edges: dict[str, set[str]] = {test: set() for test in tests}
    for test in tests:
        edges[test].update(module_files(test.removesuffix(".py").replace("/", "."), index))
        for parent in (Path(test).parent, *Path(test).parent.parents):
            rel = (parent / "conftest.py").as_posix()
            # meiva:note
            # The root conftest is not a per-test input. The selector already treats it, its
            # `pytest_plugins` modules and tests/fixtures as global, so a change to them runs the
            # full suite, and counting their production closure against every test as well
            # charged each import twice and made 327 inputs reach all tests.
            # meiva:link docs/conventions/engineering/test-selection.md
            if parent != Path() and (root / rel).is_file():
                edges[test].add(rel)
    for declaration in declarations(root):
        files = module_files(declaration.module, index) | {declaration.source}
        if (
            declaration.module.split(".", maxsplit=1)[0] in _TOPS
            and index.kind(declaration.module) is None
        ):
            incomplete[declaration.source] = (
                *incomplete.get(declaration.source, ()),
                Unknown(
                    path=declaration.source,
                    line=0,
                    expression=declaration.module,
                    reason="Declared first-party fixture module is missing",
                    kind=FactKind.IMPORT,
                ),
            )
        for path in declaration.paths:
            for test in _scope_tests(path, tests):
                edges[test].update(files)
    return edges


def plugin_modules(tree: ast.Module) -> tuple[str, ...]:
    """The literal fixture-plugin declaration, shared by classification and runtime edges."""
    modules: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, expression = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, expression = [node.target], node.value
        else:
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "pytest_plugins" for target in targets
        ):
            continue
        value: object = ast.literal_eval(expression) if expression is not None else None
        if not isinstance(value, (list, tuple)):
            raise TypeError("pytest_plugins must be a literal sequence of module-name strings")
        items = cast(list[object] | tuple[object, ...], value)
        if not all(isinstance(item, str) for item in items):
            raise TypeError("pytest_plugins must be a literal sequence of module-name strings")
        modules.extend(cast(list[str] | tuple[str, ...], items))
    return tuple(modules)


def _policy(root: Path) -> Policy:
    """The checkout's meizel policy: the declared dynamic loads and external capabilities."""
    path = root / POLICY
    if not path.is_file():
        return Policy()
    policy, problems = parse(path.read_text(encoding="utf-8"))
    if problems:
        raise ValueError(f"{POLICY} is invalid: {problems[0]}")
    return policy


# `importlib.util.find_spec` asks whether a module exists and executes none of it, so an
# unbounded target is an existence probe, not an unbounded load.
EXISTENCE_PROBE = "importlib.util.find_spec target is not bounded literal text"


def _remaining_gaps(rel: str, gaps: tuple[Unknown, ...], policy: Policy) -> tuple[Unknown, ...]:
    """The gaps of one file that its declarations do not account for.

    ``[loads]`` (dynamic loads of named directories, which become dependency edges) and
    ``[external]`` (content that is discovered at run time, with no edge) cover every gap
    except an unanchored read, which is not a load. ``[external]`` is honored for sources
    only: a test listed there may load an in-repository file by path, so it stays unbounded.
    An existence probe (``EXISTENCE_PROBE``) loads nothing and is never a gap."""
    gaps = tuple(gap for gap in gaps if gap.reason != EXISTENCE_PROBE)
    in_tests = "tests" in rel.split("/")[:-1]
    if rel in policy.loads or (rel in policy.external and not in_tests):
        return tuple(gap for gap in gaps if gap.reason == UNANCHORED_READ)
    return gaps


def build_impact(root: Path, tests: frozenset[str]) -> Impact:
    """Follow every runtime import/resource fact through source, helpers and fixtures."""
    policy = _policy(root)
    index = ModuleIndex(root)
    incomplete: dict[str, tuple[Unknown, ...]] = {}
    edges = _fixture_edges(root, tests, index, incomplete)
    member_sources: dict[Fact, set[str]] = {}
    for file in python_files(root):
        rel = file.relative_to(root).as_posix()
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=rel)
        evidence = collect(tree, rel, index, tops=_TOPS)
        dependencies = edges.setdefault(rel, set())
        for fact in evidence.records:
            if fact.kind is FactKind.RESOURCE:
                if fact.member is None:
                    dependencies.add(fact.target)
                else:
                    member_sources.setdefault(fact, set()).add(rel)
            elif fact.kind in _MODULE_FACTS:
                dependencies.update(module_files(fact.target, index))
            else:
                raise ValueError(f"Unsupported runtime dependency fact kind: {fact.kind}")
        dependencies.update(
            file for module in plugin_modules(tree) for file in module_files(module, index)
        )
        dependencies.update(policy.loads.get(rel, ()))
        incomplete[rel] = _remaining_gaps(rel, evidence.unknown, policy)

    reverse = _tests_by_input(edges, tests)
    unknown = {item for dependency in reverse for item in incomplete.get(dependency, ())}
    members = {
        fact: set[str]().union(*(reverse.get(source, set[str]()) for source in sources))
        for fact, sources in member_sources.items()
    }
    return Impact(
        reverse,
        tuple(sorted(unknown, key=lambda item: (item.path, item.line, item.expression))),
        members,
    )


def _tests_by_input(edges: dict[str, set[str]], tests: frozenset[str]) -> dict[str, set[str]]:
    """Propagate test consumers together, including cycles and shared dependencies."""
    ordered = sorted(tests)
    # Each bit is one test; unions avoid revisiting shared edges for each consumer.
    reached = {test: 1 << offset for offset, test in enumerate(ordered)}
    pending = deque(ordered)
    queued = set(ordered)
    while pending:
        source = pending.popleft()
        queued.remove(source)
        consumer_bits = reached[source]
        for dependency in edges.get(source, ()):
            before = reached.get(dependency, 0)
            after = before | consumer_bits
            if before != after:
                reached[dependency] = after
                if dependency not in queued:
                    pending.append(dependency)
                    queued.add(dependency)
    reverse: dict[str, set[str]] = {}
    for path, bits in reached.items():
        consumer_names: set[str] = set()
        remaining = bits
        while remaining:
            flag = remaining & -remaining
            consumer_names.add(ordered[flag.bit_length() - 1])
            remaining ^= flag
        reverse[path] = consumer_names
    return reverse


@contextmanager
def base_checkout(root: Path, ref: str) -> Generator[Path, None, None]:
    """Read committed base facts with the same file resolver used for the head tree."""
    commit = subprocess.run(  # noqa: S603 - argv-only revision lookup; options explicitly terminated
        ["git", "-C", str(root), "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    archive = subprocess.run(  # noqa: S603 - commit is a verified Git object identity
        ["git", "-C", str(root), "archive", "--format=tar", commit],
        check=True,
        capture_output=True,
    )
    tracked = subprocess.run(  # noqa: S603 - verified commit, no shell or user-supplied options
        ["git", "-C", str(root), "ls-tree", "-r", "--name-only", "-z", commit],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split("\0")
    with tempfile.TemporaryDirectory(prefix="meiva-test-impact-base-") as directory:
        base = Path(directory)
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as files:
            files.extractall(base, filter="data")
        omitted = [path for path in tracked if path and not os.path.lexists(base / path)]
        if omitted:
            raise RuntimeError(f"Base archive omitted tracked inputs: {omitted}")
        prepare_policy_snapshot(base)
        yield base


# meiva:note
# The pyproject.toml tables whose change cannot reach a test run: they configure checkers that
# no test run loads (each new tests/ directory registers its execution environment in
# tool.pyright). Every other table holds the interpreter, the dependencies, the pytest
# configuration or the test hosts, so the set is closed: a table that is not listed keeps the
# file global for the selector.
# meiva:link docs/conventions/engineering/test-selection.md
LOCAL_PYPROJECT_TABLES = frozenset({"tool.pyright", "tool.basedpyright", "tool.ruff"})


# meiva:note
# Whether the change to the repository's pyproject.toml since ``base_ref`` touches only
# LOCAL_PYPROJECT_TABLES, or no table at all (a comment or whitespace edit). Both committed
# texts are parsed and compared table by table: every top-level table, and ``tool.<name>`` for
# each tool. Without a base, or when the file is new or gone, the change cannot be bounded.
def pyproject_change_is_local(root: Path, base_ref: str | None) -> bool:
    head = root / "pyproject.toml"
    if base_ref is None or not head.is_file():
        return False
    shown = subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "show", "--end-of-options", f"{base_ref}:pyproject.toml"],
        check=False,
        capture_output=True,
        text=True,
    )
    if shown.returncode != 0:
        return False
    old, new = _pyproject_tables(shown.stdout), _pyproject_tables(head.read_text(encoding="utf-8"))
    absent = object()
    changed = {
        name for name in old.keys() | new.keys() if old.get(name, absent) != new.get(name, absent)
    }
    return changed <= LOCAL_PYPROJECT_TABLES


def _pyproject_tables(text: str) -> dict[str, object]:
    tables: dict[str, object] = {}
    for name, value in tomllib.loads(text).items():
        if name != "tool":
            tables[name] = value
        elif isinstance(value, dict):
            tool = cast(dict[str, object], value)
            tables.update({f"tool.{tool_name}": body for tool_name, body in tool.items()})
        else:
            raise TypeError("pyproject.toml [tool] must be a table")
    return tables


# meiva:note
# The gaps that keep a test from being certified for this change. A load the analysis cannot
# resolve can run any code, so it always blocks. A read whose path cannot be anchored can only
# matter to a data file (Python sources reach tests through the import graph, and the tests that
# read source text are the pinned tree-scan family), so it blocks only when the change includes one.
def blocking_gaps(impact: Impact, *, data_changed: bool) -> tuple[Unknown, ...]:
    return tuple(item for item in impact.unknown if data_changed or item.reason != UNANCHORED_READ)


# meiva:note
# The tests whose runtime graph reaches an input the analysis could not bound.
def unbounded_tests(impact: Impact, *, data_changed: bool) -> set[str]:
    return {
        test
        for item in blocking_gaps(impact, data_changed=data_changed)
        for test in impact.tests_by_input.get(item.path, ())
    }


def unknown_diagnostics(items: Iterable[Unknown], *, tree: str) -> tuple[str, ...]:
    """Stable, visible reasons why the runtime graph cannot certify a subset."""
    return tuple(
        f"{tree}:{item.path}:{item.line}: {item.reason}: {item.expression}" for item in items
    )


_BOUNDARY_KEYS = ("exports", "loads", "external", "state", "roots")


def _is_private_name(name: str) -> bool:
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def _manifest_doc(root: Path, directory: str) -> dict[str, object] | None:
    path = (root / directory / "meizel.toml") if directory else (root / "meizel.toml")
    if not path.is_file():
        return None
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _purity_from_doc(doc: dict[str, object], child: str) -> str | None:
    purity = doc.get("purity")
    if isinstance(purity, str):
        return purity
    if not isinstance(purity, dict):
        return None
    table = cast(dict[str, object], purity)
    child_mode = table.get(child)
    if isinstance(child_mode, str):
        return child_mode
    default_mode = table.get(".")
    return default_mode if isinstance(default_mode, str) else None


def effective_file_purity(root: Path, path: str) -> str:
    directory = path.rpartition("/")[0]
    while True:
        rel = path[len(directory) + 1 :] if directory else path
        child = rel.split("/", 1)[0]
        doc = _manifest_doc(root, directory)
        if doc is not None:
            resolved = _purity_from_doc(doc, child)
            if resolved is not None:
                return resolved
        if not directory:
            return "none"
        directory = directory.rpartition("/")[0]


def _debt_mentions_target(key: str, rel: str, child: str) -> bool:
    base = key.split("#", 1)[0].strip()
    if " -> " in base:
        src, dst = (part.strip() for part in base.split(" -> ", 1))
        return src in (rel, child) or dst in (rel, child)
    return base in (rel, child)


def _doc_has_target_debt(doc: dict[str, object], rel: str, child: str) -> bool:
    debt = doc.get("debt")
    if not isinstance(debt, dict):
        return False
    for table in cast(dict[str, object], debt).values():
        if isinstance(table, dict) and any(
            _debt_mentions_target(str(k), rel, child) for k in cast(dict[object, object], table)
        ):
            return True
    return False


def file_has_no_recorded_debt(root: Path, path: str) -> bool:
    directory = path.rpartition("/")[0]
    while True:
        rel = path[len(directory) + 1 :] if directory else path
        child = rel.split("/", 1)[0]
        doc = _manifest_doc(root, directory)
        if doc is not None and _doc_has_target_debt(doc, rel, child):
            return False
        if not directory:
            return True
        directory = directory.rpartition("/")[0]


def _manifest_boundary_unchanged(base_root: Path, head_root: Path, path: str) -> bool:
    directory = path.rpartition("/")[0]
    old_doc = _manifest_doc(base_root, directory) or {}
    new_doc = _manifest_doc(head_root, directory) or {}
    return all(old_doc.get(key) == new_doc.get(key) for key in _BOUNDARY_KEYS)


def _is_docstring(body: list[ast.stmt], index: int) -> bool:
    return (
        index == 0
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    )


def _node_names(nodes: Iterable[ast.AST | None]) -> set[str]:
    return {
        child.id
        for node in nodes
        if node is not None
        for child in ast.walk(node)
        if isinstance(child, ast.Name)
    }


def _func_signature(
    node: ast.FunctionDef | ast.AsyncFunctionDef, referenced: set[str]
) -> tuple[object, ...]:
    referenced.update(
        _node_names([*node.decorator_list, *node.type_params, node.args, node.returns])
    )
    return (
        "func",
        type(node).__name__,
        node.name,
        tuple(ast.unparse(d) for d in node.decorator_list),
        tuple(ast.unparse(tp) for tp in node.type_params),
        ast.unparse(node.args),
        ast.unparse(node.returns) if node.returns is not None else None,
    )


def _exported_list_values(node: ast.expr | None) -> set[str]:
    if not isinstance(node, (ast.List, ast.Tuple)):
        return set()
    return {
        elt.value
        for elt in node.elts
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
    }


def _ann_assign_signature(node: ast.AnnAssign, referenced: set[str]) -> tuple[object, ...] | str:
    if not isinstance(node.target, ast.Name):
        return "invalid"
    if _is_private_name(node.target.id):
        return "private"
    referenced.update(_node_names([node.annotation, node.value]))
    if node.target.id in ("__all__", "__all_for_meiva__"):
        referenced.update(_exported_list_values(node.value))
    return (
        "ann_assign",
        node.target.id,
        ast.unparse(node.annotation),
        ast.unparse(node.value) if node.value is not None else None,
    )


def _plain_assign_signature(node: ast.Assign, referenced: set[str]) -> tuple[object, ...] | str:
    if not all(isinstance(target, ast.Name) for target in node.targets):
        return "invalid"
    names = tuple(cast(ast.Name, target).id for target in node.targets)
    public = tuple(name for name in names if not _is_private_name(name))
    if not public:
        return "private"
    if len(public) != len(names):
        return "invalid"
    referenced.update(_node_names([node.value]))
    if "__all__" in public or "__all_for_meiva__" in public:
        referenced.update(_exported_list_values(node.value))
    return ("assign", public, ast.unparse(node.value))


def _assign_signature(
    node: ast.Assign | ast.AnnAssign | ast.TypeAlias, referenced: set[str]
) -> tuple[object, ...] | str:
    if isinstance(node, ast.TypeAlias):
        if _is_private_name(node.name.id):
            return "private"
        referenced.update(_node_names([*node.type_params, node.value]))
        return (
            "type_alias",
            node.name.id,
            tuple(ast.unparse(tp) for tp in node.type_params),
            ast.unparse(node.value),
        )
    if isinstance(node, ast.AnnAssign):
        return _ann_assign_signature(node, referenced)
    return _plain_assign_signature(node, referenced)


def _class_signature(node: ast.ClassDef, referenced: set[str]) -> tuple[object, ...] | None:
    members = _scope_definitions(node.body, referenced, allow_imports=False)
    if members is None:
        return None
    referenced.update(
        _node_names([*node.decorator_list, *node.type_params, *node.bases, *node.keywords])
    )
    return (
        "class",
        node.name,
        tuple(ast.unparse(d) for d in node.decorator_list),
        tuple(ast.unparse(tp) for tp in node.type_params),
        tuple(ast.unparse(b) for b in node.bases),
        tuple(ast.unparse(k) for k in node.keywords),
        members,
    )


def _scope_definitions(
    body: list[ast.stmt], referenced: set[str], *, allow_imports: bool
) -> tuple[object, ...] | None:
    items: list[object] = []
    for index, stmt in enumerate(body):
        if _is_docstring(body, index) or isinstance(stmt, ast.Pass):
            continue
        if allow_imports and isinstance(stmt, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not _is_private_name(stmt.name):
                items.append(_func_signature(stmt, referenced))
        elif isinstance(stmt, ast.ClassDef):
            if not _is_private_name(stmt.name):
                sig = _class_signature(stmt, referenced)
                if sig is None:
                    return None
                items.append(sig)
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.TypeAlias)):
            sig = _assign_signature(stmt, referenced)
            if sig == "invalid":
                return None
            if isinstance(sig, tuple):
                items.append(sig)
        else:
            return None
    return tuple(items)


def _record_alias(
    bound: str,
    target: str,
    alias: ast.alias,
    bindings: dict[str, str],
    reexports: set[tuple[str, str]],
) -> None:
    if _is_private_name(bound):
        return
    bindings[bound] = target
    if alias.asname == alias.name:
        reexports.add((bound, target))


def _import_bindings(
    tree: ast.Module,
) -> tuple[dict[str, str], frozenset[tuple[str, str]]] | None:
    bindings: dict[str, str] = {}
    reexports: set[tuple[str, str]] = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                _record_alias(
                    alias.asname or alias.name.split(".", 1)[0],
                    alias.name,
                    alias,
                    bindings,
                    reexports,
                )
        elif isinstance(stmt, ast.ImportFrom):
            if any(alias.name == "*" for alias in stmt.names):
                return None
            prefix = "." * stmt.level + (stmt.module or "")
            for alias in stmt.names:
                _record_alias(
                    alias.asname or alias.name, f"{prefix}:{alias.name}", alias, bindings, reexports
                )
    return bindings, frozenset(reexports)


def _module_interface_fingerprint(
    tree: ast.Module, *, is_package_init: bool
) -> tuple[object, ...] | None:
    referenced: set[str] = set()
    defs = _scope_definitions(tree.body, referenced, allow_imports=True)
    imports = _import_bindings(tree)
    if defs is None or imports is None:
        return None
    bindings, reexports = imports
    if is_package_init:
        interface_imports = tuple(sorted(bindings.items()))
    else:
        used = {(name, bindings[name]) for name in referenced if name in bindings} | set(reexports)
        interface_imports = tuple(sorted(used))
    if not defs and not interface_imports:
        return None
    return (defs, interface_imports)


def public_interface_unchanged(base_root: Path, head_root: Path, path: str) -> bool:
    base_file, head_file = base_root / path, head_root / path
    if not (path.endswith(".py") and base_file.is_file() and head_file.is_file()):
        return False
    if (
        effective_file_purity(base_root, path) != "strict"
        or effective_file_purity(head_root, path) != "strict"
        or not file_has_no_recorded_debt(base_root, path)
        or not file_has_no_recorded_debt(head_root, path)
        or not _manifest_boundary_unchanged(base_root, head_root, path)
    ):
        return False
    try:
        base_tree = ast.parse(base_file.read_text(encoding="utf-8"), filename=path)
        head_tree = ast.parse(head_file.read_text(encoding="utf-8"), filename=path)
    except (OSError, SyntaxError, ValueError):
        return False
    is_init = path.endswith("/__init__.py")
    base_fp = _module_interface_fingerprint(base_tree, is_package_init=is_init)
    head_fp = _module_interface_fingerprint(head_tree, is_package_init=is_init)
    return base_fp is not None and base_fp == head_fp


def package_local_tests(
    path: str, test_dirs: frozenset[str], collectable: frozenset[str]
) -> set[str]:
    directory = path.rpartition("/")[0]
    while directory:
        tests_dir = directory if directory.rpartition("/")[2] == "tests" else f"{directory}/tests"
        if tests_dir in test_dirs:
            return {test for test in collectable if test.startswith(f"{tests_dir}/")}
        directory = directory.rpartition("/")[0]
    return set()


class _CheckoutScope(Protocol):
    @property
    def repo_root(self) -> Path: ...
    @property
    def test_dirs(self) -> frozenset[str]: ...
    @property
    def collectable(self) -> frozenset[str]: ...


def qualifies_for_interface_cutoff(
    path: str,
    *,
    base: _CheckoutScope,
    head: _CheckoutScope,
    base_impact: Impact,
    head_impact: Impact,
) -> bool:
    if "tests" in path.split("/")[:-1] or Path(path).name == "conftest.py":
        return False
    head_local = package_local_tests(path, head.test_dirs, head.collectable)
    base_local = package_local_tests(path, base.test_dirs, base.collectable)
    if not (head_local & head_impact.runtime_tests(path)) or not (
        base_local & base_impact.runtime_tests(path)
    ):
        return False
    return public_interface_unchanged(base.repo_root, head.repo_root, path)


def apply_interface_cutoff(
    package_paths: Iterable[str],
    base: _CheckoutScope,
    head: _CheckoutScope,
    base_impact: Impact,
    head_impact: Impact,
) -> tuple[Impact, Impact]:
    cutoff = frozenset(
        path
        for path in package_paths
        if qualifies_for_interface_cutoff(
            path, base=base, head=head, base_impact=base_impact, head_impact=head_impact
        )
    )
    return replace(base_impact, cutoff=cutoff), replace(head_impact, cutoff=cutoff)
