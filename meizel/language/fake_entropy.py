# meiva:note
# Where a file uses a reading that is not entropy as an identifier or a random draw.
#
# An interval clock (`time.monotonic`, `time.perf_counter` and their `_ns` forms), an object address
# (`id(x)`) and a process-local counter (`next(itertools.count())`) look like a fresh value on every
# call, so code reaches for them where it needs an identifier or a random number: a run id from
# `time.monotonic_ns()`, an idempotency key from `perf_counter_ns()` and a sequence number, a
# linear-congruential draw over a clock reading. They are not entropy: two processes started
# together draw the same value, a restart repeats a counter, and an address is reused once an
# object is gone. And because they are neither the wall clock nor a random call, they pass the
# `ambient-read` rule that `uuid.uuid4()` and `random.random()` fail. This pass finds the use, by
# where the reading goes, and reports it as the randomness it stands in for.
#
# A reading is *spent* when it reaches one of: a format (`f"{x}"`, `str`, `hex`, `format`, `repr`),
# a digest or a name derived from it (`hash`, `hashlib.*`, `uuid.uuid5`, `uuid.UUID`), a seed
# (`random.Random`, `random.seed`), a keyword that names an identity (`run_id=`, `key=`, `seed=`),
# or a remainder and bit operation (`% n`, `& mask`, `<< k`). A difference of two readings is a
# duration and carries nothing: `end - start` is the plain use of an interval clock. The result of a
# remainder or bit operation is the draw itself and is not followed further. A process-local counter
# is spent only in the first kinds, since `n % period` is how a sampler counts.
#
# The second shape is a module's own wrapper over the clock: a private zero-argument function whose
# body is `return time.monotonic()` and which no code uses as a value. A wrapper that a parameter
# default or an argument names (`now: Callable[[], float] = _monotonic`) is an injection point and
# is left alone; one that is only called is the clock read under a name that hides it from the
# rule on sleeping functions, and a seam only a patch can reach.
from __future__ import annotations

import ast
import re
from collections.abc import Callable, Iterator

from meizel.graph.model import EntropySite

__all__ = [
    "COUNTER_FACTORIES",
    "ENTROPY_CLOCKS",
    "function_sites",
    "is_loop_clock",
    "value_wrapper",
    "wrapper_clock",
]

ENTROPY_CLOCKS = frozenset(
    {"time.monotonic", "time.monotonic_ns", "time.perf_counter", "time.perf_counter_ns"}
)
COUNTER_FACTORIES = frozenset({"itertools.count"})
_LOOP_GETTERS = frozenset({"asyncio.get_running_loop", "asyncio.get_event_loop"})
_LOOP_NAMES = frozenset({"loop", "_loop", "event_loop"})
_LOOP = "loop.time"
_FORMATS = frozenset({"str", "repr", "hex", "oct", "bin", "format", "hash"})
_DIGESTS = frozenset(
    {
        "hashlib.md5",
        "hashlib.sha1",
        "hashlib.sha256",
        "hashlib.sha512",
        "hashlib.blake2b",
        "uuid.uuid3",
        "uuid.uuid5",
        "uuid.UUID",
        "random.Random",
        "random.seed",
    }
)
_CARRIERS = frozenset({"int", "float", "abs", "round", *_FORMATS})
_IDENTITY_KEYWORD = re.compile(r"(?:^|_)(?:id|ids|key|seed|nonce|salt|token|uuid)$")
_METHODS = frozenset({"hex", "to_bytes", "encode"})
_BITS = (ast.Mod, ast.BitAnd, ast.BitOr, ast.BitXor, ast.LShift, ast.RShift)
_NESTED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_COUNTER = "itertools.count"
_ADDRESS = "id"

type Dotted = Callable[[ast.expr], str | None]
type Unbound = Callable[[str], bool]


def _own(body: list[ast.stmt]) -> Iterator[ast.AST]:
    pending: list[ast.AST] = list(reversed(body))
    while pending:
        node = pending.pop()
        yield node
        if not isinstance(node, _NESTED):
            pending.extend(reversed(list(ast.iter_child_nodes(node))))


def _last(expr: ast.expr) -> str:
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return expr.id if isinstance(expr, ast.Name) else ""


# meiva:note
# `loop.time()` on `asyncio.get_running_loop()` or on a name that says it is the loop: the event
# loop's monotonic clock, an interval clock like `time.monotonic`.
def is_loop_clock(call: ast.Call, dotted: Dotted) -> bool:
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr == "time" and not call.args):
        return False
    holder = func.value
    if isinstance(holder, ast.Call):
        return dotted(holder.func) in _LOOP_GETTERS
    return _last(holder) in _LOOP_NAMES


# meiva:note
# What one scope's readings do, judged with the names the file resolves.
class _Spend:
    def __init__(self, dotted: Dotted, unbound: Unbound, counters: frozenset[str]) -> None:
        self.dotted = dotted
        self.unbound = unbound
        self.counters = counters
        self.names: dict[str, str] = {}

    # meiva:note
    # The dotted name a call goes to; a builtin that nothing shadows is named by itself.
    def callee(self, func: ast.expr) -> str | None:
        found = self.dotted(func)
        if found is None and isinstance(func, ast.Name) and self.unbound(func.id):
            return func.id
        return found

    def source(self, node: ast.expr) -> str | None:
        if not isinstance(node, ast.Call):
            return None
        callee = self.callee(node.func)
        if callee in ENTROPY_CLOCKS:
            return callee
        if is_loop_clock(node, self.dotted):
            return _LOOP
        if not isinstance(node.func, ast.Name) or callee != node.func.id:
            return None
        if node.func.id == _ADDRESS and len(node.args) == 1:
            return _ADDRESS
        if node.func.id == "next" and node.args and _last(node.args[0]) in self.counters:
            return _COUNTER
        return None

    # meiva:note
    # The source a value derives from; a difference is a duration and derives from none.
    def reading(self, expr: ast.expr) -> str | None:
        found = self.source(expr)
        if found is not None:
            return found
        if isinstance(expr, ast.Name):
            return self.names.get(expr.id)
        if isinstance(expr, ast.Attribute):
            return self.names.get(f".{expr.attr}")
        if isinstance(expr, ast.BinOp) and not isinstance(expr.op, (ast.Sub, *_BITS)):
            return self.reading(expr.left) or self.reading(expr.right)
        if isinstance(expr, ast.UnaryOp):
            return self.reading(expr.operand)
        if isinstance(expr, ast.IfExp):
            return self.reading(expr.body) or self.reading(expr.orelse)
        if isinstance(expr, ast.Call) and self.callee(expr.func) in _CARRIERS:
            return next((r for a in expr.args if (r := self.reading(a))), None)
        return None

    def learn(self, nodes: list[ast.AST]) -> None:
        for _ in range(2):
            for node in nodes:
                targets, value = _assignment(node)
                reading = None if value is None else self.reading(value)
                if reading is None:
                    continue
                for target in targets:
                    if isinstance(target, ast.Name):
                        self.names[target.id] = reading
                    elif isinstance(target, ast.Attribute):
                        self.names[f".{target.attr}"] = reading


def _assignment(node: ast.AST) -> tuple[list[ast.expr], ast.expr | None]:
    if isinstance(node, ast.Assign):
        return node.targets, node.value
    if isinstance(node, ast.AnnAssign | ast.AugAssign | ast.NamedExpr) and node.value is not None:
        return [node.target], node.value
    return [], None


# meiva:note
# The reading among a call's arguments, positional or keyword: `uuid.UUID(int=id(self))` and
# `"{}".format(stamp)` spend it as surely as `str(stamp)` does.
def _first_reading(call: ast.Call, spend: _Spend) -> str | None:
    values = [*call.args, *(keyword.value for keyword in call.keywords)]
    return next((reading for value in values if (reading := spend.reading(value))), None)


def _spent(call: ast.Call, spend: _Spend) -> Iterator[tuple[str, str]]:
    callee = spend.callee(call.func)
    formats = isinstance(call.func, ast.Attribute) and call.func.attr == "format"
    if callee in _FORMATS or callee in _DIGESTS or formats:
        reading = _first_reading(call, spend)
        if reading is not None:
            yield reading, ".format()" if formats else str(callee)
    for keyword in call.keywords:
        if keyword.arg is not None and _IDENTITY_KEYWORD.search(keyword.arg):
            reading = spend.reading(keyword.value)
            if reading is not None:
                yield reading, f"{keyword.arg}="
    if isinstance(call.func, ast.Attribute) and call.func.attr in _METHODS:
        reading = spend.reading(call.func.value)
        if reading is not None:
            yield reading, f".{call.func.attr}()"


# meiva:note
# The readings one scope's own statements spend as identity or randomness. `body` is a function
# body or a module body; nested functions and classes are their own scopes.
def function_sites(
    body: list[ast.stmt], dotted: Dotted, unbound: Unbound, counters: frozenset[str]
) -> list[EntropySite]:
    nodes = list(_own(body))
    spend = _Spend(dotted, unbound, counters)
    spend.learn(nodes)
    found: set[EntropySite] = set()
    for node in nodes:
        if isinstance(node, ast.FormattedValue):
            reading = spend.reading(node.value)
            if reading is not None:
                found.add(EntropySite(node.lineno, f"{reading} in an f-string"))
        elif isinstance(node, ast.Call):
            found.update(EntropySite(node.lineno, f"{r} in {c}") for r, c in _spent(node, spend))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, _BITS):
            reading = spend.reading(node.left) or spend.reading(node.right)
            if reading is not None and reading != _COUNTER:
                found.add(EntropySite(node.lineno, f"{reading} in `{_operator(node.op)}`"))
    return sorted(found, key=lambda site: (site.line, site.what))


_OPERATORS: dict[type[ast.operator], str] = {
    ast.Mod: "%",
    ast.BitAnd: "&",
    ast.BitOr: "|",
    ast.BitXor: "^",
    ast.LShift: "<<",
    ast.RShift: ">>",
}


def _operator(op: ast.operator) -> str:
    return _OPERATORS[type(op)]


# meiva:note
# The clock an expression reads when it is a call without arguments: an interval clock by name, or
# the loop's.
def _clock_call(expr: ast.expr, dotted: Dotted) -> str | None:
    if not isinstance(expr, ast.Call) or expr.args or expr.keywords:
        return None
    callee = dotted(expr.func)
    if callee in ENTROPY_CLOCKS:
        return callee
    return _LOOP if is_loop_clock(expr, dotted) else None


# meiva:note
# The clock a function is a one-line wrapper over: a private function without parameters (a method
# with only `self` or `cls`) whose only statement is `return <interval clock>()`, or None.
def wrapper_clock(
    function: ast.FunctionDef | ast.AsyncFunctionDef, dotted: Dotted, *, method: bool = False
) -> str | None:
    args = function.args
    own = [arg.arg for arg in args.args]
    receiver = method and own in (["self"], ["cls"])
    if (
        not function.name.startswith("_")
        or (own and not receiver)
        or args.kwonlyargs
        or args.vararg
    ):
        return None
    body = [
        statement
        for statement in function.body
        if not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant))
    ]
    if len(body) != 1 or not isinstance(body[0], ast.Return) or body[0].value is None:
        return None
    return _clock_call(body[0].value, dotted)


# meiva:note
# The clock a module-level value is under another name: `_now = time.monotonic`, `_now = lambda:
# time.monotonic()` and `_now = functools.partial(time.monotonic)`. A call of it reads the clock
# and no signature says so.
def value_wrapper(value: ast.expr, dotted: Dotted) -> str | None:
    if isinstance(value, ast.Name | ast.Attribute):
        name = dotted(value)
        return name if name in ENTROPY_CLOCKS else None
    if isinstance(value, ast.Lambda):
        args = value.args
        bare = not (args.args or args.posonlyargs or args.kwonlyargs or args.vararg or args.kwarg)
        return _clock_call(value.body, dotted) if bare else None
    if (
        isinstance(value, ast.Call)
        and dotted(value.func) == "functools.partial"
        and len(value.args) == 1
        and not value.keywords
    ):
        name = dotted(value.args[0])
        return name if name in ENTROPY_CLOCKS else None
    return None
