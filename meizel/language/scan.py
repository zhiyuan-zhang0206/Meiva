"""Fact extraction from one first-party Python file, without importing it."""

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from meizel import allowances
from meizel.graph.imports import (
    Scope,
    absolute_base,
    collect_scope,
    dotted_chain,
    parameter_names,
)
from meizel.graph.model import (
    Acquisition,
    EnumSubsetUse,
    Fact,
    FactKind,
    MeizelError,
    Patch,
    Problem,
    PurityItem,
    Restricted,
    Sources,
)
from meizel.graph.universe import Universe
from meizel.language import fixtures, purity, typescript
from meizel.language.ambient import AmbientAccess
from meizel.language.bindings import (
    is_imported_binding,
    module_bindings,
    unambiguous_function_lines,
)
from meizel.language.input_mutations import InputListAppend, input_list_appends
from meizel.language.mounts import mount_facts
from meizel.language.restricted import EXTERNAL_FORMS, const_str

__all__ = [
    "EXTERNAL_FORMS",
    "FileScan",
    "invalidate_policy_caches",
    "is_imported_binding",
    "module_bindings",
    "mount_facts",
    "scan_file",
    "scan_source",
]

# The calls that start a process, with where the argument list is: the index of the list argument,
# or the index where an unpacked list starts. A bare `python` counts as a command in these only.
_ARGV_LIST_AT = {
    "subprocess.run": 0,
    "subprocess.call": 0,
    "subprocess.check_call": 0,
    "subprocess.check_output": 0,
    "subprocess.Popen": 0,
    **dict.fromkeys(
        allowances.load(Path(__file__).resolve().parents[2]).names(
            "python-commands", "argv-wrappers"
        ),
        0,
    ),
    "os.execv": 1,
    "os.execve": 1,
    "os.execvp": 1,
    "os.execvpe": 1,
    "os.spawnv": 2,
    "os.spawnve": 2,
    "os.spawnvp": 2,
    "os.spawnvpe": 2,
}
_ARGV_UNPACKED_FROM = {
    "asyncio.create_subprocess_exec": 0,
    "os.execl": 1,
    "os.execle": 1,
    "os.execlp": 1,
    "os.execlpe": 1,
    "os.spawnl": 2,
    "os.spawnle": 2,
    "os.spawnlp": 2,
    "os.spawnlpe": 2,
}
_LEAVES = frozenset(
    {
        ast.Constant,
        ast.Load,
        ast.Store,
        ast.Del,
        ast.And,
        ast.Or,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.MatMult,
        ast.Div,
        ast.Mod,
        ast.Pow,
        ast.LShift,
        ast.RShift,
        ast.BitOr,
        ast.BitXor,
        ast.BitAnd,
        ast.FloorDiv,
        ast.Invert,
        ast.Not,
        ast.UAdd,
        ast.USub,
        ast.Eq,
        ast.NotEq,
        ast.Lt,
        ast.LtE,
        ast.Gt,
        ast.GtE,
        ast.Is,
        ast.IsNot,
        ast.In,
        ast.NotIn,
        ast.Pass,
        ast.Break,
        ast.Continue,
    }
)


@dataclass(frozen=True, slots=True)
class FileScan:
    path: str
    facts: list[Fact] = field(default_factory=list[Fact])
    restricted: list[Restricted] = field(default_factory=list[Restricted])
    patches: list[Patch] = field(default_factory=list[Patch])
    problems: list[Problem] = field(default_factory=list[Problem])
    fixtures: fixtures.FixtureState = field(default_factory=fixtures.FixtureState)
    imported: set[str] = field(default_factory=set[str])
    purity: list[PurityItem] = field(default_factory=list[PurityItem])
    enum_subsets: list[EnumSubsetUse] = field(default_factory=list[EnumSubsetUse])
    input_appends: list[InputListAppend] = field(default_factory=list[InputListAppend])
    ambient: list[Acquisition] = field(default_factory=list[Acquisition])


class Scanner(AmbientAccess):
    def __init__(
        self, universe: Universe, path: str, tree: ast.Module, *, walrus: bool, sources: Sources
    ) -> None:
        super().__init__(universe, path, tree, walrus=walrus, sources=sources)
        self.fixture_state = fixtures.FixtureState()
        self.fn_depth = 0
        self.owned_functions = unambiguous_function_lines(tree)
        self.fixture_requester: ast.FunctionDef | ast.AsyncFunctionDef | None = None
        self.input_appends: list[InputListAppend] = []
        self.star_import = any(
            isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names)
            for n in ast.walk(tree)
        )
        self._handlers: dict[type[ast.AST], Callable[[Any], None]] = {
            ast.Import: self.visit_import,
            ast.ImportFrom: self.visit_import_from,
            ast.Attribute: self.visit_attribute,
            ast.Name: self.visit_name,
            ast.Call: self.visit_call,
            ast.Subscript: self.visit_subscript,
            ast.FunctionDef: self.visit_function,
            ast.AsyncFunctionDef: self.visit_function,
            ast.ClassDef: self.visit_class_def,
            ast.Assign: self.visit_assign,
            ast.AugAssign: self.visit_store,
            ast.AnnAssign: self.visit_store,
            ast.Delete: self.visit_store,
            ast.Expr: self.visit_expr,
            ast.Compare: self.visit_compare,
            ast.List: self.visit_sequence,
            ast.Tuple: self.visit_sequence,
        }

    def run(self) -> FileScan:
        self.children(self.tree)
        self.purity_state.record_module(self.tree)
        return FileScan(
            self.path,
            facts=list(dict.fromkeys(self.facts)),
            restricted=self.restricted,
            patches=self.patches,
            problems=self.problems,
            fixtures=self.fixture_state,
            imported=self.imported,
            purity=self.purity_state.items,
            enum_subsets=self.purity_state.finalize_subsets(),
            input_appends=self.input_appends,
            ambient=self.ambient.items,
        )

    def visit_compare(self, node: ast.Compare) -> None:
        purity.record_enum_subsets(self.purity_state.enum_subsets, node)
        self.children(node)

    def visit_import(self, node: ast.Import) -> None:
        super().visit_import(node)
        self.purity_state.record_import(node)

    def visit_import_from(self, node: ast.ImportFrom) -> None:
        super().visit_import_from(node)
        self.purity_state.record_import_from(node)
        base = absolute_base(node, self.package)
        if self.records_ambient and base is not None:
            for alias in node.names:
                self.ambient.imported(f"{base}.{alias.name}" if base else alias.name, node.lineno)

    def visit_name(self, node: ast.Name) -> None:
        super().visit_name(node)
        if isinstance(node.ctx, ast.Load):
            origin, _ = self.scope.find(node.id)
            self.purity_state.record_env_access(origin, node.lineno)
            if self.records_ambient and origin is not None:
                self.ambient.used(origin, origin, node.lineno)

    # --- traversal -----------------------------------------------------------------

    def visit(self, node: ast.AST) -> None:
        handler = self._handlers.get(type(node))
        if handler is None:
            self.children(node)
        else:
            handler(node)

    def children(self, node: ast.AST) -> None:
        handlers = self._handlers
        for name in node._fields:
            value = getattr(node, name, None)
            if isinstance(value, list):
                for child in cast("list[object]", value):
                    if isinstance(child, ast.AST) and type(child) not in _LEAVES:
                        handler = handlers.get(type(child))
                        handler(child) if handler else self.children(child)
            elif isinstance(value, ast.AST) and type(value) not in _LEAVES:
                handler = handlers.get(type(value))
                handler(value) if handler else self.children(value)

    def visit_attribute(self, node: ast.Attribute) -> None:
        chain = dotted_chain(node)
        if chain is None:
            self.children(node)
            return
        base, attrs = chain
        origin, _ = self.scope.find(base.id)
        if origin is None:
            return
        self.restricted_attribute(origin, attrs, node.lineno)
        self.module_uses(origin, attrs, node.lineno)
        self.purity_state.record_env_access(".".join([origin, *attrs]), node.lineno)
        if self.records_ambient:
            self.ambient.used(".".join([origin, *attrs]), origin, node.lineno)

    # --- scopes --------------------------------------------------------------------

    def visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        previous_requester, self.fixture_requester = self.fixture_requester, node
        args = node.args
        for deco in node.decorator_list:
            self.visit(deco)
        for default in (*args.defaults, *(d for d in args.kw_defaults if d is not None)):
            self.visit(default)
        for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg):
            if arg is not None and arg.annotation is not None:
                self.visit(arg.annotation)
        if node.returns is not None:
            self.visit(node.returns)
        fixtures.record_function(
            self.fixture_state,
            node,
            self.dotted,
            test_file=self.test_file,
            top_level=self.fn_depth == 0,
            module_owned=node.lineno in self.owned_functions,
        )
        purity.record_function(
            self.purity_state,
            node,
            top_level=self.fn_depth == 0 and not self.scope.is_class,
        )
        parent = self.scope.parent if self.scope.is_class else self.scope
        inner = Scope(parent)
        collect_scope(inner, node.body, self.package, walrus=self.walrus)
        self.observe_input_parameters(node, inner)
        inner.shadowed.update(parameter_names(args))
        self.fn_depth += 1
        self.ambient.enter(node)
        self.enter(inner, node.body)
        self.ambient.leave()
        self.fn_depth -= 1
        self.fixture_requester = previous_requester

    def observe_input_parameters(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, inner: Scope
    ) -> None:
        if self.fn_depth == 0 and self.scope.parent is None:
            self.input_appends.extend(
                input_list_appends(
                    node,
                    inner,
                    builtin_list=not self.star_import and self.scope.find("list") == (None, False),
                )
            )

    def visit_class_def(self, node: ast.ClassDef) -> None:
        previous_requester, self.fixture_requester = self.fixture_requester, None
        self.record_test_subclass(node)
        for outer in (*node.decorator_list, *node.bases, *node.keywords):
            self.visit(outer)
        purity.record_class(self.purity_state, node, self.dotted)
        inner = Scope(self.scope, is_class=True)
        collect_scope(inner, node.body, self.package, walrus=self.walrus)
        self.enter(inner, node.body)
        self.fixture_requester = previous_requester

    def enter(self, inner: Scope, body: list[ast.stmt]) -> None:
        outer, self.scope = self.scope, inner
        for statement in body:
            self.visit(statement)
        self.scope = outer

    # --- statements ----------------------------------------------------------------

    def visit_expr(self, node: ast.Expr) -> None:
        if const_str(node.value) is None:
            self.children(node)

    def visit_assign(self, node: ast.Assign) -> None:
        self.stores(node.targets, node.lineno, "assignment")
        purity.record_assign(self.purity_state, node, self.dotted)
        at_module = self.scope.parent is None
        if at_module and any(
            isinstance(t, ast.Name) and t.id == "pytest_plugins" for t in node.targets
        ):
            names = fixtures.plugin_names(node.value)
            if names is None:
                self.restrict(node.lineno, "pytest_plugins that is not a literal")
            else:
                self.fixture_state.plugins.extend((n, node.lineno) for n in names)
                for name in names:
                    self.plugin_fact(name, node.lineno)
        self.children(node)

    def visit_store(self, node: ast.AugAssign | ast.AnnAssign | ast.Delete) -> None:
        if isinstance(node, ast.Delete):
            self.stores(node.targets, node.lineno, "del")
        elif not isinstance(node, ast.AnnAssign) or node.value is not None:
            self.stores([node.target], node.lineno, "assignment")
            if isinstance(node, ast.AnnAssign):
                purity.record_assign(self.purity_state, node, self.dotted)
        self.children(node)

    # --- attribute writes ----------------------------------------------------------

    def stores(self, targets: list[ast.expr], line: int, form: str) -> None:
        """Writes through an imported name are patches, exactly as `monkeypatch.setattr` is."""
        for target in targets:
            if isinstance(target, ast.Tuple | ast.List):
                self.stores(target.elts, line, form)
            elif isinstance(target, ast.Starred):
                self.stores([target.value], line, form)
            elif isinstance(target, ast.Attribute):
                self.written(target.value, (target.attr,), line, form)
                purity.record_attr_write(self.purity_state.items, target, line)
            elif isinstance(target, ast.Subscript):
                self.dict_write(target, line)

    def plugin_fact(self, dotted: str, line: int) -> None:
        module = self.u.resolve_module(dotted) if self.u.is_first_party_name(dotted) else None
        if module is not None and module.file is not None:
            self.add(module.file, line, FactKind.PLUGIN, (), f"pytest_plugins {dotted}")

    # --- calls ---------------------------------------------------------------------

    def visit_call(self, node: ast.Call) -> None:
        callee = self.dotted(node.func)
        if self.records_ambient:
            self.ambient.called(callee, node)
        self.patch_forms(node, callee)
        self.spawn_call(callee, node)
        fixtures.record_call(self.fixture_state, node, callee, requester=self.fixture_requester)
        unshadowed_open = (
            isinstance(node.func, ast.Name)
            and node.func.id == "open"
            and self.scope.find("open") == (None, False)
        )
        purity.record_call(
            self.purity_state, node, callee, self.dotted, unshadowed_open=unshadowed_open
        )
        self.children(node)

    def spawn_call(self, callee: str | None, node: ast.Call) -> None:
        """A call that starts a process: judge its argument list when that starts a Python interpreter."""
        at = _ARGV_LIST_AT.get(callee or "")
        if at is not None and len(node.args) > at:
            argv = node.args[at]
            if isinstance(argv, ast.List | ast.Tuple):
                self.interpreter_command(argv.elts, node.lineno, bare=True)
            return
        start = _ARGV_UNPACKED_FROM.get(callee or "")
        if start is not None:
            self.interpreter_command(node.args[start:], node.lineno)

    def visit_subscript(self, node: ast.Subscript) -> None:
        self.sys_modules_write(node)
        self.children(node)

    # --- python -c -----------------------------------------------------------------

    def visit_sequence(self, node: ast.List | ast.Tuple) -> None:
        self.interpreter_command(node.elts, node.lineno, bare=False)
        self.children(node)


def scan_source(universe: Universe, path: str, source: bytes, sources: Sources) -> FileScan:
    """Scan `source` as if it were the file at `path` (a version that is not in the working tree)."""
    if path.endswith((".ts", ".tsx")):
        ts = typescript.scan_typescript(path, source.decode("utf-8", errors="replace"), universe)
        return FileScan(
            path,
            facts=list(ts.facts),
            patches=list(ts.patches),
            problems=list(ts.problems),
            imported=set(ts.external),
            purity=list(ts.purity),
        )
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as error:
        raise MeizelError(f"{path}:{error.lineno}: cannot parse: {error.msg}") from error
    except ValueError as error:
        raise MeizelError(f"{path}: cannot read: {error}") from error
    return Scanner(universe, path, tree, walrus=b":=" in source, sources=sources).run()


def scan_file(universe: Universe, path: str, sources: Sources) -> FileScan:
    try:
        source = (universe.root / path).read_bytes()
    except (OSError, ValueError) as error:
        raise MeizelError(f"{path}: cannot read: {error}") from error
    return scan_source(universe, path, source, sources)


# meiva:note
# This is the scanner boundary for a disposable snapshot whose policy has changed. Each cache
# owner invalidates its own entries; callers neither reach into private caches nor skip the
# original snapshot validation to avoid them.
def invalidate_policy_caches() -> None:
    allowances.invalidate_cache()
    purity.invalidate_policy_cache()
