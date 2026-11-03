"""Trace schema definitions through source imports without booting the gateway."""

import ast
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

type NodeBinding = tuple[Literal["node"], ast.stmt]
type ImportBinding = tuple[Literal["import"], str, str]
type ModuleBinding = tuple[Literal["module"], str]
type ConditionalBinding = tuple[Literal["conditional"], int]
type Binding = NodeBinding | ImportBinding | ModuleBinding
type ModuleBindings = dict[str, Binding | ConditionalBinding]
type FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


class SourceGraph:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.visited: set[tuple[str, str]] = set()
        self.classes: set[tuple[str, str]] = set()
        self.paths: dict[str, Path | None] = {}
        self.trees: dict[str, list[ast.stmt]] = {}
        self.module_bindings: dict[str, ModuleBindings] = {}

    def path(self, module: str) -> Path | None:
        if module not in self.paths:
            base = self.root.joinpath(*module.split("."))
            self.paths[module] = next(
                (p for p in (base.with_suffix(".py"), base / "__init__.py") if p.is_file()), None
            )
        return self.paths[module]

    def nodes(self, module: str) -> list[ast.stmt]:
        if module not in self.trees:
            path = self.path(module)
            self.trees[module] = ast.parse(path.read_text()).body if path else []
        return self.trees[module]

    def import_target(self, module: str, node: ast.ImportFrom) -> str:
        if not node.level:
            assert node.module is not None, "absolute from-import without a module"
            return node.module
        module_path = self.path(module)
        assert module_path is not None, f"relative import from an unresolved module: {module}"
        package = module if module_path.name == "__init__.py" else module.rpartition(".")[0]
        parts = package.split(".")
        return ".".join(
            parts[: len(parts) - node.level + 1] + ([node.module] if node.module else [])
        )

    def bindings(self, module: str) -> ModuleBindings:
        if module not in self.module_bindings:
            self.module_bindings[module] = self.read_bindings(module)
        return self.module_bindings[module]

    def read_bindings(self, module: str) -> ModuleBindings:
        result: ModuleBindings = {}
        for node in self.nodes(module):
            if isinstance(node, (ast.ImportFrom, ast.Import)):
                result.update(self.import_bindings(module, node))
            elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                result[node.name] = ("node", node)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        result[target.id] = ("node", node)
            elif isinstance(
                node, (ast.If, ast.Try, ast.TryStar, ast.With, ast.For, ast.While, ast.Match)
            ):
                result.update(self.conditional_bindings(module, node))
        return result

    def conditional_bindings(self, module: str, node: ast.stmt) -> ModuleBindings:
        """Track uncertain globals; reject only when schema traversal uses one."""
        result: ModuleBindings = {}
        pending: list[ast.AST] = [node]
        while pending:
            current = pending.pop()
            names: Iterable[str] = []
            if isinstance(current, (ast.Import, ast.ImportFrom)):
                names = self.import_bindings(module, current)
            elif isinstance(current, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                names = [current.name]  # Do not descend into local scopes.
            elif isinstance(current, (ast.Assign, ast.AnnAssign)):
                targets = current.targets if isinstance(current, ast.Assign) else [current.target]
                names = [target.id for target in targets if isinstance(target, ast.Name)]
            else:
                pending.extend(ast.iter_child_nodes(current))
            if names:
                assert isinstance(current, ast.stmt)
                result.update(dict.fromkeys(names, ("conditional", current.lineno)))
        return result

    def binding(self, module: str, name: str) -> Binding | None:
        info = self.bindings(module).get(name)
        assert not info or info[0] != "conditional", (
            f"Unsupported conditional schema binding: {module}.{name}"
        )
        return info

    def import_bindings(self, module: str, node: ast.Import | ast.ImportFrom) -> dict[str, Binding]:
        if isinstance(node, ast.ImportFrom):
            target = self.import_target(module, node)
            assert all(alias.name != "*" for alias in node.names), (
                f"Unsupported star import: {module}"
            )
            return {
                alias.asname or alias.name: ("import", target, alias.name) for alias in node.names
            }
        return {
            alias.asname or alias.name.split(".")[0]: (
                "module",
                alias.name if alias.asname else alias.name.split(".")[0],
            )
            for alias in node.names
        }

    def symbol(self, module: str, name: str) -> None:
        key = module, name
        if key in self.visited:
            return
        self.visited.add(key)
        info = self.binding(module, name)
        if not info:
            return
        if info[0] == "import":
            self.symbol(info[1], info[2])
            return
        if info[0] == "module":
            return
        node = info[1]
        if isinstance(node, ast.ClassDef):
            self.classes.add(key)
            for base in node.bases:
                self.expression(module, base)
            for field in node.body:
                if isinstance(field, ast.AnnAssign):
                    self.expression(module, field.annotation)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value:
            self.expression(module, node.value)

    def qualified(self, module: str, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            binding = self.binding(module, node.id)
            if binding and binding[0] == "module":
                return binding[1]
            if binding and binding[0] == "import":
                return self.imported_module(binding[1], binding[2], set())
        if isinstance(node, ast.Attribute):
            value = self.qualified(module, node.value)
            if value:
                return self.imported_module(value, node.attr, set())
        return None

    def imported_module(self, module: str, name: str, seen: set[tuple[str, str]]) -> str | None:
        """Resolve package module aliases before interpreting an attribute."""
        key = module, name
        assert key not in seen, f"Cyclic module re-export: {key}"
        seen.add(key)
        binding = self.binding(module, name)
        if binding and binding[0] == "module":
            return binding[1]
        if binding and binding[0] == "import" and (binding[1], binding[2]) != key:
            return self.imported_module(binding[1], binding[2], seen)
        candidate = f"{module}.{name}"
        return candidate if self.path(candidate) else None

    def expression(self, module: str, node: ast.AST) -> None:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name):
                self.symbol(module, sub.id)
            elif isinstance(sub, ast.Attribute):
                target = self.qualified(module, sub.value)
                if target:
                    self.symbol(target, sub.attr)
            elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                try:
                    quoted = ast.parse(sub.value, mode="eval")
                except SyntaxError:
                    continue
                # Literal strings such as "queued" may parse as names, but
                # only actual bindings can resolve to classes.
                if not isinstance(quoted.body, ast.Constant):
                    self.expression(module, quoted)

    @staticmethod
    def route_decorators(func: FunctionNode) -> list[ast.Call]:
        methods = {"get", "post", "put", "delete", "patch", "head", "options", "api_route"}
        return [
            decorator
            for decorator in func.decorator_list
            if isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr in methods
        ]

    def route_annotations(self, func: FunctionNode) -> list[ast.expr]:
        decorators = self.route_decorators(func)
        if not decorators:
            return []
        annotations = [
            keyword.value
            for decorator in decorators
            for keyword in decorator.keywords
            if keyword.arg in {"response_model", "responses"}
        ]
        if func.returns:
            annotations.append(func.returns)
        annotations.extend(
            arg.annotation
            for arg in func.args.posonlyargs + func.args.args + func.args.kwonlyargs
            if arg.annotation
        )
        return annotations

    def route_classes(self) -> set[tuple[str, str]]:
        # Routes live in `gateway/routers/` (single-module surfaces) and in the
        # gateway's feature packages (`gateway/agents/`, `gateway/cluster/`,
        # `gateway/inspect/`, ...), all mounted by gateway/app.py — so every
        # gateway module is scanned for route decorators.
        for path in (self.root / "gateway").rglob("*.py"):
            if "tests" in path.relative_to(self.root).parts:
                continue  # a gateway test's throwaway routes are not the wire schema
            parts = path.relative_to(self.root).with_suffix("").parts
            module = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
            for func in self.nodes(module):
                if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for annotation in self.route_annotations(func):
                    self.expression(module, annotation)
        return self.classes

    def relative_source(self, module: str) -> str:
        path = self.path(module)
        assert path is not None, f"module without a source file: {module}"
        return str(path.relative_to(self.root))

    def schema_sources(self, components: Iterable[str], generated: Iterable[str]) -> set[str]:
        origins: defaultdict[str, set[str]] = defaultdict(set)
        for module, name in self.route_classes():
            origins[name].add(module)
        # Name indexes are used only after reachability has established
        # actual defining module identities. Never search all repository
        # classes by name. Ambiguities fail rather than guessing a source.
        unknown = set(components) - origins.keys() - set(generated)
        ambiguous = {name: sorted(origins[name]) for name in components if len(origins[name]) > 1}
        assert not unknown, f"Unresolved components: {sorted(unknown)}"
        assert not ambiguous, f"Ambiguous reachable components: {ambiguous}"
        # Include newly added models and inherited fields before regeneration.
        return {self.relative_source(module) for module, _name in self.classes}

    def imported_sources(self, start: str) -> set[str]:
        seen: set[str] = set()
        pending = [start]
        while pending:
            module = pending.pop()
            if module in seen or self.path(module) is None:
                continue
            seen.add(module)
            for node in ast.walk(ast.Module(body=self.nodes(module), type_ignores=[])):
                if isinstance(node, ast.Import):
                    pending.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    target = self.import_target(module, node)
                    pending.append(target)
                    pending.extend(target + "." + alias.name for alias in node.names)
        return {self.relative_source(module) for module in seen}
