# meiva:note
# The timing-judgement rule: a test may not judge a duration by the real wall clock. A bound loose
# enough not to flake on a loaded runner is loose enough to pass the defect it was written to catch
# (#7725 installed a probe defect and `elapsed < 0.5` stayed green with it), and a lower bound fails
# a correct implementation that got faster. Silence is bought per line with
# `# timing-guard: <only catches a hang | injected clock>`; the sites that predate the rule live in
# `meizel/owners/timing_judgement_baseline.json` keyed by path, function and comparison with its
# literals masked. The rule's parts, the three legitimate answers when it fires and what it
# deliberately does not judge are in meizel/docs/lint/timing-judgement.meiva.okf.md.

from __future__ import annotations

import ast
from collections.abc import Iterator
from itertools import pairwise

MARKER = "# timing-guard:"
# meiva:note
# `monotonic`, `perf_counter`, `process_time` and `time` are clock attributes no library reuses for
# anything else. `.time()` is the exception: it is the clock on a time module or a loop, and
# `queue.time()` would be someone else's method, so that one also checks the owner's name.
# `from time import monotonic` binds the same read to a bare name, so the import is read too: a
# clock judged only when it is spelled `time.<attr>()` would leave every bare import ungoverned
# (2026-10-30: `_until_sessions` in test_daemon_entry spends 30 s of wall clock that way).
_CLOCK_ATTRS = frozenset({"monotonic", "perf_counter", "process_time", "time"})
_CLOCK_OWNERS = frozenset({"time", "loop", "clock"})
Site = tuple[str, int, ast.Compare, ast.FunctionDef | ast.AsyncFunctionDef]


def _imported_clocks(tree: ast.Module) -> frozenset[str]:
    # meiva:note
    # The bare names `from time import monotonic` (or `... as now`) binds in this module.
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "time":
            names.update(
                alias.asname or alias.name for alias in node.names if alias.name in _CLOCK_ATTRS
            )
    return frozenset(names)


def _is_clock_call(node: ast.AST, bare: frozenset[str] = frozenset()) -> bool:
    # meiva:note
    # True for `time.monotonic()`, `loop.time()`, `self._loop.time()`, their family, and a clock
    # this module imported by name.
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return node.func.id in bare
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    attr = node.func.attr
    if attr not in _CLOCK_ATTRS:
        return False
    if attr != "time":
        return True
    owner = node.func.value
    name = owner.id if isinstance(owner, ast.Name) else getattr(owner, "attr", "")
    return name in _CLOCK_OWNERS or name.endswith("loop")


def _own_nodes(node: ast.AST) -> Iterator[ast.AST]:
    # meiva:note
    # This node and its descendants, without entering a nested function or class.
    roots = node.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else [node]
    pending: list[ast.AST] = list(roots)
    while pending:
        node = pending.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                pending.append(child)


def _clock_names(fn: ast.FunctionDef | ast.AsyncFunctionDef, bare: frozenset[str]) -> set[str]:
    # meiva:note
    # Names bound to a clock read, or to a difference of two of them, to a fixed point.
    names: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in _own_nodes(fn):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            if not _is_clock_quantity(node.value, names, bare):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in names:
                    names.add(target.id)
                    changed = True
    return names


def _is_clock_quantity(node: ast.AST, names: set[str], bare: frozenset[str]) -> bool:
    # meiva:note
    # A clock read, a name bound to one, or a sum/difference of those -- a duration or an instant.
    #
    # Deliberately not "any expression that mentions a clock": `x = await wait(timeout=clock())`
    # carries a clock into `x` without making `x` one, and comparing `x == 3` says nothing about
    # time.
    #
    if isinstance(node, ast.Name):
        return node.id in names
    if _is_clock_call(node, bare):
        return True
    if isinstance(node, ast.UnaryOp):
        return _is_clock_quantity(node.operand, names, bare)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Sub, ast.Add)):
        return _is_clock_quantity(node.left, names, bare) or _is_clock_quantity(
            node.right, names, bare
        )
    return False


def _is_number(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (int, float)) and not isinstance(node.value, bool)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return _is_number(node.operand)
    if isinstance(node, ast.BinOp):
        return _is_number(node.left) and _is_number(node.right)
    return False


def _judges_a_clock(node: ast.AST, names: set[str], bare: frozenset[str]) -> ast.Compare | None:
    # meiva:note
    # The first comparison under `node` that puts a clock quantity against a number.
    for sub in _own_nodes(node):
        if not isinstance(sub, ast.Compare):
            continue
        sides = [sub.left, *sub.comparators]
        for left, right in pairwise(sides):
            if (_is_number(right) and _is_clock_quantity(left, names, bare)) or (
                _is_number(left) and _is_clock_quantity(right, names, bare)
            ):
                return sub
    return None


class _MaskNumbers(ast.NodeTransformer):
    # meiva:note
    # Replace every numeric literal with `N`: the shape is what is compared, not the value.

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return ast.copy_location(ast.Name(id="N", ctx=ast.Load()), node)
        return node


def _normalise(compare: ast.Compare) -> str:
    # meiva:note
    # The comparison with its literals masked, so retuning one keeps its baseline entry.
    fresh = ast.parse(ast.unparse(compare), mode="eval").body
    return ast.unparse(_MaskNumbers().visit(fresh))


def _functions(tree: ast.Module) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _commented(lines: list[str], lineno: int) -> bool:
    # meiva:note
    # The marker on the assert's own line, or on the line directly above it.
    return any(
        0 <= index < len(lines) and MARKER in lines[index] for index in (lineno - 1, lineno - 2)
    )


def _sites(src: str, rel_path: str) -> list[Site]:
    # meiva:note
    # Every timing judgement in one file that carries no marker.
    try:
        tree = ast.parse(src)
    except SyntaxError:
        # meiva:note
        # A file that does not parse is another lint's finding, not this one's.
        return []
    lines = src.splitlines()
    bare = _imported_clocks(tree)
    sites: list[Site] = []
    for fn in _functions(tree):
        names = _clock_names(fn, bare)
        if not names:
            continue
        for node in _own_nodes(fn):
            if not isinstance(node, ast.Assert) or _commented(lines, node.lineno):
                continue
            compare = _judges_a_clock(node.test, names, bare)
            if compare is not None:
                sites.append((rel_path, node.lineno, compare, fn))
    return sites


def _message(compare: ast.Compare, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    return (
        f"{fn.name} judges a duration by the wall clock (`{ast.unparse(compare)}`): a bound "
        "loose enough not to flake passes the defect it was written to catch. Assert a counted "
        "fact instead (envelopes sent, rounds run, the answer the code returned), or mark it "
        "`# timing-guard: <only catches a hang | injected clock>`."
    )


def findings_in_source(
    src: str, rel_path: str, *, baseline: frozenset[tuple[str, str, str]] = frozenset()
) -> list[tuple[int, str]]:
    # meiva:note
    # Findings for one file: judgements that carry no marker and are not in the baseline.
    return [
        (line, _message(compare, fn))
        for rel, line, compare, fn in _sites(src, rel_path)
        if (rel, fn.name, _normalise(compare)) not in baseline
    ]


def site_keys(src: str, rel_path: str) -> set[tuple[str, str, str]]:
    # meiva:note
    # The baseline key of every judgement in one file that carries no marker.
    return {(rel, fn.name, _normalise(compare)) for rel, _, compare, fn in _sites(src, rel_path)}


def findings(
    files: list[tuple[str, str]],
    *,
    baseline: frozenset[tuple[str, str, str]] = frozenset(),
    scope: frozenset[str] | None = None,
) -> list[tuple[str, int, str]]:
    # meiva:note
    # Every finding in `(relative path, text)` pairs, honouring an optional changed-file scope.
    return [
        (rel, line, message)
        for rel, text in files
        if scope is None or rel in scope
        for line, message in findings_in_source(text, rel, baseline=baseline)
    ]
