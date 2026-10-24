# meiva:note
# A deferred Thread constructor is admitted only by a visible, single-generation protocol.
# This proof is deliberately local: straight-line guards and retention under one owner Lock,
# followed immediately by native start outside it. Unknown control flow stays a finding.
from __future__ import annotations

import ast

from meizel.state.ambient_state.module import Module, dotted
from meizel.state.ambient_state.owner_method import (
    Function,
    assignment_pairs,
    call_nodes,
    executable_nodes,
    instance_field,
)


def _assignment(statement: ast.stmt) -> tuple[ast.expr, ast.expr] | None:
    if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
        return statement.targets[0], statement.value
    if isinstance(statement, ast.AnnAssign) and statement.value is not None:
        return statement.target, statement.value
    return None


def _initial(methods: dict[str, Function], field: str) -> tuple[ast.expr, ast.expr] | None:
    init = methods.get("__init__")
    if init is None:
        return None
    pairs = [
        pair for st in init.body if (pair := _assignment(st)) and instance_field(pair[0]) == field
    ]
    return pairs[0] if len(pairs) == 1 else None


def _writes(methods: dict[str, Function], field: str) -> set[int]:
    found: set[int] = set()
    for method in methods.values():
        aliases = {
            target.id
            for target, value in assignment_pairs(method)
            if isinstance(target, ast.Name) and instance_field(value) == field
        }
        for node in executable_nodes(method):
            if not isinstance(node, ast.Attribute | ast.Subscript) or not isinstance(
                node.ctx, ast.Store | ast.Del
            ):
                continue
            name = dotted(node.value if isinstance(node, ast.Subscript) else node)
            if name and (
                name == field or name.startswith(field + ".") or name.split(".")[0] in aliases
            ):
                found.add(id(node))
    return found


def _bare_instance(node: ast.AST) -> bool:
    if isinstance(node, ast.Name) and node.id == "self":
        return True
    if isinstance(node, ast.Attribute) and (dotted(node) or "").startswith("self."):
        return False
    return any(_bare_instance(child) for child in ast.iter_child_nodes(node))


def _lock_exposed(method: Function, lock: str) -> bool:
    contexts = {
        id(item.context_expr)
        for node in executable_nodes(method)
        if isinstance(node, ast.With)
        for item in node.items
    }
    return any(
        isinstance(node, ast.Attribute)
        and instance_field(node) == lock
        and isinstance(node.ctx, ast.Load)
        and id(node) not in contexts
        for node in ast.walk(method)
    )


def _signal_exposed(method: Function, signal: str, worker: str) -> bool:
    callees = {id(call.func) for call in call_nodes(method)}
    receivers = {
        id(call.func.value) for call in call_nodes(method) if isinstance(call.func, ast.Attribute)
    }
    for node in ast.walk(method):
        if not isinstance(node, ast.Attribute):
            continue
        if instance_field(node.value) == signal and (
            id(node) not in callees or node.attr not in {"set", "wait", "is_set"}
        ):
            return True
        if (
            instance_field(node) == signal
            and isinstance(node.ctx, ast.Load)
            and id(node) not in receivers
            and method.name != worker
        ):
            return True
    return False


def _escaping_value(node: ast.AST, signal: str) -> bool:
    if not isinstance(
        node,
        ast.Assign
        | ast.AnnAssign
        | ast.AugAssign
        | ast.NamedExpr
        | ast.Return
        | ast.Yield
        | ast.YieldFrom,
    ):
        return False
    return node.value is not None and (
        instance_field(node.value) == signal or _bare_instance(node.value)
    )


def _instance_escapes(method: Function, signal: str) -> bool:
    for node in ast.walk(method):
        if (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda)
            and node is not method
        ):
            return True
        if isinstance(node, ast.Attribute) and (
            node.attr == "__dict__" or instance_field(node) == "self.__init__"
        ):
            return True
        if isinstance(node, ast.Call) and any(
            _bare_instance(argument)
            for argument in [*node.args, *(kw.value for kw in node.keywords)]
        ):
            return True
        if _escaping_value(node, signal):
            return True
    return False


def _unexposed(methods: dict[str, Function], lock: str, signal: str, worker: str) -> bool:
    return not any(
        _lock_exposed(method, lock)
        or _signal_exposed(method, signal, worker)
        or _instance_escapes(method, signal)
        for method in methods.values()
    )


def _constructed(methods: dict[str, Function], field: str, qualified: str, module: Module) -> bool:
    initial = _initial(methods, field)
    if initial is None:
        return False
    target, value = initial
    return (
        isinstance(value, ast.Call)
        and module.full_name(value.func) == qualified
        and _unshadowed(value.func, methods["__init__"], module)
        and not value.args
        and not value.keywords
        and _writes(methods, field) == {id(target)}
    )


def _bound_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
        return node.id
    if isinstance(node, ast.arg):
        return node.arg
    if isinstance(node, ast.alias):
        return node.asname or node.name.split(".")[0]
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.ExceptHandler):
        return node.name
    return None


def _unshadowed(callee: ast.expr, method: Function, module: Module) -> bool:
    name = dotted(callee)
    binding = name.split(".")[0] if name else ""
    if binding not in module.imports or binding in module.defs:
        return False
    return not any(
        isinstance(node, ast.Match) or (node is not method and _bound_name(node) == binding)
        for node in ast.walk(method)
    )


def _lock(block: ast.stmt) -> str | None:
    if isinstance(block, ast.With) and len(block.items) == 1:
        item = block.items[0]
        return instance_field(item.context_expr) if item.optional_vars is None else None
    return None


def _field_call(node: ast.AST, field: str, name: str) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and instance_field(node.func.value) == field
        and node.func.attr == name
    )


def _is_not_none(test: ast.expr, field: str) -> bool:
    return (
        isinstance(test, ast.Compare)
        and instance_field(test.left) == field
        and len(test.ops) == len(test.comparators) == 1
        and isinstance(test.ops[0], ast.IsNot)
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value is None
    )


def _guard(statement: ast.stmt, field: str, *, signal: bool) -> bool:
    if not isinstance(statement, ast.If) or statement.orelse or len(statement.body) != 1:
        return False
    if not isinstance(statement.body[0], ast.Return | ast.Raise):
        return False
    test = statement.test
    if signal:
        return (
            _field_call(test, field, "is_set")
            and isinstance(test, ast.Call)
            and not (test.args or test.keywords)
        )
    return _is_not_none(test, field)


def _locked_sets(method: Function, signal: str, lock: str) -> set[int]:
    protected: set[int] = set()
    for block in method.body:
        if _lock(block) != lock or not isinstance(block, ast.With):
            continue
        for statement in block.body:
            if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
                continue
            call = statement.value
            if _field_call(call, signal, "set") and not call.args and not call.keywords:
                protected.add(id(call))
    return protected


def _closed_under_lock(methods: dict[str, Function], signal: str, lock: str) -> bool:
    sets: set[int] = set()
    protected: set[int] = set()
    for method in methods.values():
        for call in call_nodes(method):
            if _field_call(call, signal, "clear"):
                return False
            if _field_call(call, signal, "set"):
                sets.add(id(call))
        protected.update(_locked_sets(method, signal, lock))
    return bool(sets) and sets == protected


def _following_start(method: Function, index: int) -> ast.expr | None:
    if index + 1 >= len(method.body):
        return None
    statement = method.body[index + 1]
    if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
        return None
    call = statement.value
    if (
        not isinstance(call.func, ast.Attribute)
        or call.func.attr != "start"
        or call.args
        or call.keywords
    ):
        return None
    return call.func.value


def _local_retention(method: Function, statement: ast.stmt, held: str, name: str) -> bool:
    alias = _assignment(statement)
    if alias is None or not isinstance(alias[0], ast.Name) or alias[0].id != name:
        return False
    if instance_field(alias[1]) != held:
        return False
    if any(
        isinstance(node, ast.Global | ast.Nonlocal) and name in node.names
        for node in executable_nodes(method)
    ):
        return False
    writes = [
        n
        for n in executable_nodes(method)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store | ast.Del) and n.id == name
    ]
    return writes == [alias[0]]


def _starts_retained(
    method: Function, block: ast.With, index: int, held: str, constructor_index: int
) -> bool:
    receiver = _following_start(method, index)
    if receiver is None:
        return False
    if instance_field(receiver) == held:
        return len(block.body) == constructor_index + 1
    if not isinstance(receiver, ast.Name) or len(block.body) != constructor_index + 2:
        return False
    return _local_retention(method, block.body[-1], held, receiver.id)


def _none_handle(methods: dict[str, Function], held: str) -> ast.expr | None:
    initial = _initial(methods, held)
    if initial is None or not isinstance(initial[1], ast.Constant) or initial[1].value is not None:
        return None
    return initial[0]


def _construction_position(
    call: ast.Call, block: ast.With, held: str, initial: ast.expr, methods: dict[str, Function]
) -> int | None:
    for position, child in enumerate(block.body):
        pair = _assignment(child)
        if pair is None or pair[1] is not call or instance_field(pair[0]) != held:
            continue
        if _writes(methods, held) != {id(initial), id(pair[0])}:
            return None
        before = block.body[:position]
        if len(before) != 2 or not _guard(before[1], held, signal=False):
            return None
        return position
    return None


def _signals_closed(
    call: ast.Call,
    methods: dict[str, Function],
    signals: set[str],
    module: Module,
    lock: str,
    guard: ast.stmt,
) -> bool:
    target = next((kw.value for kw in call.keywords if kw.arg == "target"), None)
    worker = instance_field(target) if target is not None else None
    if worker is None:
        return False
    return any(
        _constructed(methods, signal, "threading.Event", module)
        and _unexposed(methods, lock, signal, worker.removeprefix("self."))
        and _guard(guard, signal, signal=True)
        and _closed_under_lock(methods, signal, lock)
        for signal in signals
    )


def admitted(
    call: ast.Call,
    method: Function,
    methods: dict[str, Function],
    held: str,
    signals: set[str],
    module: Module,
) -> bool:
    if not isinstance(method, ast.FunctionDef):
        return False
    if not _unshadowed(call.func, method, module):
        return False
    if any(helper.decorator_list for helper in methods.values()):
        return False
    initial = _none_handle(methods, held)
    if initial is None:
        return False
    for index, statement in enumerate(method.body):
        lock = _lock(statement)
        if (
            not lock
            or not isinstance(statement, ast.With)
            or not _constructed(methods, lock, "threading.Lock", module)
        ):
            continue
        position = _construction_position(call, statement, held, initial, methods)
        if position is None:
            continue
        if not _starts_retained(method, statement, index, held, position):
            return False
        return _signals_closed(call, methods, signals, module, lock, statement.body[0])
    return False
