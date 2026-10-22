from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Iterator, Mapping

_CONTEXT = "base.agents.context.MeivaContext"
_RUNTIME = "langgraph.runtime.Runtime"
_AGENT = "base.host.env.agent_slices.AgentSlices"
_AUTHORITY = "base.config.service_read.ConfigAuthority"
_AUTHORITY_GETTER = "meiva.sdk_surface.settings.config_authority"
_BOOT = "base.config.ConfigBoot"
_BOOT_VIEW = "base.config.ConfigBoot.view"

type _ScopeNode = (
    ast.FunctionDef
    | ast.AsyncFunctionDef
    | ast.ClassDef
    | ast.Lambda
    | ast.ListComp
    | ast.SetComp
    | ast.DictComp
    | ast.GeneratorExp
)
_SCOPE_NODES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Lambda,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)
_BINDING_NODES = (
    ast.Name,
    ast.Assign,
    ast.AnnAssign,
    ast.Import,
    ast.ImportFrom,
    ast.Attribute,
    ast.For,
    ast.AsyncFor,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.ExceptHandler,
)


def _scope_parts(node: _ScopeNode) -> tuple[tuple[ast.AST, ...], tuple[ast.AST, ...]]:
    if isinstance(node, ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp):
        outer = (node.generators[0].iter,)
        inner: list[ast.AST] = []
        for offset, generator in enumerate(node.generators):
            if offset:
                inner.append(generator.iter)
            inner.extend((generator.target, *generator.ifs))
        inner.extend((node.key, node.value) if isinstance(node, ast.DictComp) else (node.elt,))
        return outer, tuple(inner)
    if isinstance(node, ast.ClassDef):
        outer = (*node.decorator_list, *node.bases, *node.keywords, *node.type_params)
        return outer, tuple(node.body)
    if isinstance(node, ast.Lambda):
        return (node.args,), (node.body,)
    outer = (node.args, *node.decorator_list, *node.type_params)
    if node.returns is not None:
        outer = (*outer, node.returns)
    return outer, tuple(node.body)


def _local_nodes(tree: ast.AST) -> Iterator[ast.AST]:
    children = (
        iter(_scope_parts(tree)[1])
        if isinstance(tree, _SCOPE_NODES)
        else ast.iter_child_nodes(tree)
    )
    pending = [(children, False)]
    while pending:
        children, scope_root = pending[-1]
        child = next(children, None)
        if child is None:
            pending.pop()
            continue
        if isinstance(child, ast.Name):
            if isinstance(child.ctx, ast.Store):
                yield child
            continue
        if isinstance(child, ast.Constant) or not child._fields:
            continue
        if isinstance(child, _BINDING_NODES):
            yield child
        if isinstance(child, _SCOPE_NODES):
            outer, inner = _scope_parts(child)
            pending.append((iter(inner if scope_root else outer), not scope_root))
        elif child._fields:
            pending.append((ast.iter_child_nodes(child), False))


def _import_origins(node: ast.Import | ast.ImportFrom, path: str) -> dict[str, str]:
    if isinstance(node, ast.Import):
        return {
            (alias.asname or alias.name.split(".")[0]): (
                alias.name if alias.asname else alias.name.split(".")[0]
            )
            for alias in node.names
        }
    if node.level:
        if not path:
            return {}
        package = path.removesuffix(".py").split("/")[:-1]
        if node.level > len(package):
            return {}
        anchor = package[: len(package) - (node.level - 1)]
        base = ".".join([*anchor, *([node.module] if node.module else [])])
    else:
        base = node.module or ""
    return {
        (alias.asname or alias.name): (f"{base}.{alias.name}" if base else alias.name)
        for alias in node.names
        if alias.name != "*"
    }


class _Scope:
    def __init__(self, tree: ast.AST, path: str, parent: _Scope | None = None) -> None:
        self.parent = parent
        self.is_class = isinstance(tree, ast.ClassDef)
        self.values: dict[str, ast.expr] = {}
        self.origins: dict[str, str] = {}
        self.import_counts: Counter[str] = Counter()
        self.attribute_writes: list[ast.Attribute] = []
        self.stores: Counter[str] = Counter()
        for node in _local_nodes(tree):
            self._bind(node, path)
        if isinstance(tree, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            args = tree.args
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
                self.stores[arg.arg] += 1
            for arg in (args.vararg, args.kwarg):
                if arg is not None:
                    self.stores[arg.arg] += 1

    def _bind(self, node: ast.AST, path: str) -> None:
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            self.stores[node.id] += 1
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            self._bind_assignment(node)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            self._bind_import(node, path)
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store | ast.Del):
            self.attribute_writes.append(node)
        elif (
            isinstance(
                node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.ExceptHandler
            )
            and node.name is not None
        ):
            self.stores[node.name] += 1

    def _bind_assignment(self, node: ast.Assign | ast.AnnAssign) -> None:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.values[target.id] = node.value
        elif isinstance(node.target, ast.Name) and node.value:
            self.values[node.target.id] = node.value

    def _bind_import(self, node: ast.Import | ast.ImportFrom, path: str) -> None:
        for name, origin in _import_origins(node, path).items():
            previous = self.origins.get(name, origin)
            self.origins[name] = origin if previous == origin else ""
            self.import_counts[name] += 1
            self.stores[name] += 1

    def unmodified_origin(self, node: ast.expr) -> str:
        origin = self.origin(node)
        scope: _Scope | None = self
        while origin and scope is not None:
            for written in scope.attribute_writes:
                target = scope.origin(written)
                if target and (origin == target or origin.startswith(target + ".")):
                    return ""
            scope = scope.parent
        return origin

    def value(self, node: ast.expr) -> ast.expr:
        if not isinstance(node, ast.Name):
            return node
        if node.id not in self.stores and self.parent is not None:
            return self.parent.value(node)
        return self.values.get(node.id, node) if self.stores[node.id] == 1 else node

    def origin(self, node: ast.expr, seen: frozenset[str] = frozenset()) -> str:
        if isinstance(node, ast.Attribute):
            base = self.origin(node.value, seen)
            return f"{base}.{node.attr}" if base else ""
        if not isinstance(node, ast.Name) or node.id in seen:
            return ""
        if node.id not in self.stores and self.parent is not None:
            return self.parent.origin(node, seen)
        if self.stores[node.id] == 1 and node.id in self.values:
            return self.origin(self.values[node.id], seen | {node.id})
        return (
            self.origins.get(node.id, "")
            if self.stores[node.id] == self.import_counts[node.id]
            else ""
        )

    def nested_parent(self) -> _Scope:
        scope = self
        while scope.is_class and scope.parent is not None:
            scope = scope.parent
        return scope


class _ReaderFacts(ast.NodeVisitor):
    def __init__(self, tree: ast.AST, fields: Mapping[str, str], path: str) -> None:
        self.path = path
        self.parents = {
            id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        self.fields = fields
        self.scope = _Scope(tree, path)
        self.annotations: dict[_Scope, dict[str, tuple[ast.expr, _Scope]]] = {}
        self.reads: list[tuple[str, str | None]] = []

    def _annotation(self, node: ast.expr, scope: _Scope) -> str:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node = ast.parse(node.value, mode="eval").body
        if isinstance(node, ast.Subscript):
            owner = scope.unmodified_origin(node.value)
            argument = self._annotation(node.slice, scope)
            return f"{owner}[{argument}]" if owner and argument else ""
        return scope.unmodified_origin(node)

    def _written(self, node: ast.expr, scope: _Scope) -> bool:
        expression = ast.unparse(node)
        current: _Scope | None = scope
        while current is not None:
            for written in current.attribute_writes:
                target = ast.unparse(written)
                if expression == target or expression.startswith(target + "."):
                    return True
            current = current.parent
        return False

    def _type(self, node: ast.expr, scope: _Scope, seen: frozenset[str] = frozenset()) -> str:
        if self._written(node, scope):
            return ""
        if isinstance(node, ast.Name):
            return self._bound_type(node, scope, seen)
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "context"
            and self._type(node.value, scope, seen) == f"{_RUNTIME}[{_CONTEXT}]"
        ):
            return _CONTEXT
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "view"
            and self._type(node.value, scope, seen) == _BOOT
        ):
            return _BOOT_VIEW
        if isinstance(node, ast.Call):
            return self._call_type(node, scope, seen)
        return ""

    def _bound_type(self, node: ast.Name, scope: _Scope, seen: frozenset[str]) -> str:
        if node.id in seen:
            return ""
        if node.id not in scope.stores and scope.parent is not None:
            return self._type(node, scope.parent, seen)
        if scope.stores[node.id] != 1:
            return ""
        value = scope.value(node)
        if value is not node:
            return self._type(value, scope, seen | {node.id})
        annotation = self.annotations.get(scope, {}).get(node.id)
        return self._annotation(*annotation) if annotation else ""

    def _call_type(self, node: ast.Call, scope: _Scope, seen: frozenset[str]) -> str:
        if self._written(node.func, scope):
            return ""
        origin = scope.unmodified_origin(node.func)
        if origin == _BOOT:
            return _BOOT
        if node.args or node.keywords:
            return ""
        if origin == _AUTHORITY_GETTER:
            return _AUTHORITY
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "require_agent"
            and self._type(node.func.value, scope, seen) == _CONTEXT
        ):
            return _AGENT
        return ""

    def _nested(self, node: _ScopeNode) -> None:
        parent = self.scope
        outer, inner = _scope_parts(node)
        for expression in outer:
            self.visit(expression)
        self.scope = _Scope(node, self.path, parent.nested_parent())
        annotations: dict[str, tuple[ast.expr, _Scope]] = {}
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                if arg.annotation is not None:
                    annotations[arg.arg] = (arg.annotation, parent)
        self.annotations[self.scope] = annotations
        for statement in inner:
            self.visit(statement)
        self.scope = parent

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._nested(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._nested(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._nested(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._nested(node)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._nested(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._nested(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._nested(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._nested(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name):
            self.annotations.setdefault(self.scope, {})[node.target.id] = (
                node.annotation,
                self.scope,
            )
        self.generic_visit(node)

    def _receiver_key(self, node: ast.expr, scope: _Scope) -> str:
        if isinstance(node, ast.Name):
            if node.id not in scope.stores and scope.parent is not None:
                return self._receiver_key(node, scope.parent)
            value = scope.value(node)
            if value is not node and isinstance(value, ast.Name | ast.Attribute):
                return self._receiver_key(value, scope)
            return f"{id(scope)}:{node.id}"
        if isinstance(node, ast.Attribute):
            return f"{self._receiver_key(node.value, scope)}.{node.attr}"
        return str(id(node))

    def _guarded_view(self, node: ast.Attribute, domain: str) -> bool:
        receiver = node.value.value if isinstance(node.value, ast.Attribute) else node.value
        key = self._receiver_key(receiver, self.scope)
        child: ast.AST = node
        parent = self.parents.get(id(child))
        while parent is not None:
            if isinstance(parent, ast.If) and child in parent.body:
                test = parent.test
                if (
                    isinstance(test, ast.Call)
                    and isinstance(test.func, ast.Attribute)
                    and test.func.attr == "has_domain"
                    and self._type(test.func.value, self.scope) == _BOOT_VIEW
                    and self._receiver_key(test.func.value, self.scope) == key
                    and _literal_argument(test, 0, "domain") == domain
                ):
                    return True
            child, parent = parent, self.parents.get(id(parent))
        return False

    def _assert_view_field(self, domain: str, field: str) -> None:
        if field in self.fields:
            valid = self.fields[field] == domain
        else:
            from base.config.service_read import domain_model_classes

            owner = domain_model_classes().get(domain)
            valid = owner is not None and hasattr(owner, field)
        assert valid, f"config read names an undeclared field: {domain}.{field}"

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if (
            isinstance(node.ctx, ast.Load)
            and isinstance(node.value, ast.Attribute)
            and self._type(node.value.value, self.scope) == _BOOT_VIEW
            and not self._written(node, self.scope)
        ):
            domain, field = node.value.attr, node.attr
            self._assert_view_field(domain, field)
            if not self._guarded_view(node, domain):
                self.reads.append((domain, field))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute) and not self._written(node.func, self.scope):
            receiver = self._type(node.func.value, self.scope)
            if receiver == _AUTHORITY and node.func.attr == "service_field_value":
                field = _literal_argument(node, 0, "name")
                if field is not None:
                    assert field in self.fields, f"config read names an undeclared field: {field}"
                    self.reads.append((self.fields[field], field))
            elif receiver == _AGENT and node.func.attr == "read":
                domain = _literal_argument(node, 0, "domain")
                field = _literal_argument(node, 1, "field")
                if domain is not None:
                    assert domain in self.fields.values(), (
                        f"config read names an undeclared domain: {domain}"
                    )
                    if field is not None:
                        assert self.fields.get(field) == domain, (
                            f"config read names an undeclared field: {domain}.{field}"
                        )
                    self.reads.append((domain, field))
        self.generic_visit(node)


def _literal_argument(node: ast.Call, position: int, name: str) -> str | None:
    argument = (
        node.args[position]
        if len(node.args) > position
        else next((keyword.value for keyword in node.keywords if keyword.arg == name), None)
    )
    return (
        argument.value
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
        else None
    )


def explicit_config_reads(source: str, path: str = "") -> list[tuple[str, str | None]]:
    from base.host.env.config_lite_table import FIELD_DOMAINS

    tree = ast.parse(source)
    collector = _ReaderFacts(tree, FIELD_DOMAINS, path)
    collector.visit(tree)
    return collector.reads
