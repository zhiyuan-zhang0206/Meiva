# meiva:note
# The module view the rules ask: lazy ASTs, top-level functions, fixed-instant
# names, and the per-function summary rule 2 quotes. A module is parsed only
# when a rule reaches it, and the answer lives for the run.
# meiva:link scripts/lint/docs/time-bomb-rule-parts.meiva.okf.md

from __future__ import annotations

import ast
import re
from pathlib import Path

from scripts.lint.diagnostics.time_bomb_rule.sources import (
    CLOCK_PARAMS,
    is_real_now_call,
)
from scripts.structure.lazy_modules import ModuleMap


def _is_dt_ctor(node: ast.AST) -> bool:
    if isinstance(node, ast.Call):
        f = node.func
        if isinstance(f, ast.Name) and f.id in ("datetime", "date"):
            return True
        if (
            isinstance(f, ast.Attribute)
            and isinstance(f.value, ast.Name)
            and f.value.id == "datetime"
            and f.attr in ("datetime", "date")
        ):
            return True
    return False


_FIXED_CTOR_LOOSE = re.compile(r"date(?:time)?\s*\(")
_FIXED_CTOR_HINT = re.compile(r"\b(?:datetime|date)\s*\(")

_Functions = dict[str, tuple[ast.FunctionDef | ast.AsyncFunctionDef, frozenset[str]]]


class _FixedMap(ModuleMap[frozenset[str]]):
    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.get(name) is not None


def _top_level_functions(tree: ast.Module) -> _Functions:
    functions: _Functions = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            functions[node.name] = (node, frozenset(a.arg for a in args))
    return functions


def _module_assignments(tree: ast.Module) -> list[tuple[str, ast.AST]]:
    found: list[tuple[str, ast.AST]] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            found.extend((t.id, node.value) for t in node.targets if isinstance(t, ast.Name))
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            found.append((node.target.id, node.value))
    return found


def _fixed_instant_names(tree: ast.Module) -> frozenset[str]:
    assigns = _module_assignments(tree)
    names = {name for name, value in assigns if _is_dt_ctor(value)}
    while derived := {
        name
        for name, value in assigns
        if name not in names
        and any(isinstance(n, ast.Name) and n.id in names for n in ast.walk(value))
    }:
        names |= derived
    return frozenset(names)


class Index:
    def __init__(self, root: Path, dirs: tuple[str, ...]) -> None:
        self.root = root
        self._scanned = frozenset((*dirs, "tests"))
        self._exists: dict[str, bool] = {}
        self._trees: dict[str, ast.Module | None] = {}
        self.trees = ModuleMap(self._is_module, self._tree)
        self.fns = ModuleMap(self._is_module, self._functions)
        self.fixed = _FixedMap(self._is_module, self._fixed_names)
        self._summary: dict[tuple[str, str], tuple[bool, bool, bool]] = {}

    def functions(self, mod: str) -> _Functions:
        return self.fns.get(mod) or {}

    def fixed_names(self, mod: str) -> frozenset[str]:
        return self.fixed.get(mod) or frozenset()

    def _path(self, mod: str) -> Path:
        return self.root / (mod.replace(".", "/") + ".py")

    def _is_module(self, mod: str) -> bool:
        if mod not in self._exists:
            relative = mod.replace(".", "/")
            self._exists[mod] = (
                mod.split(".", 1)[0] in self._scanned
                and "/mirrors/" not in relative
                and "__pycache__" not in relative
                and self._path(mod).is_file()
            )
        return self._exists[mod]

    def _tree(self, mod: str) -> ast.Module | None:
        if mod not in self._trees:
            try:
                self._trees[mod] = ast.parse(self._path(mod).read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, SyntaxError):
                self._trees[mod] = None
        return self._trees[mod]

    def _functions(self, mod: str) -> _Functions | None:
        tree = self._tree(mod)
        return None if tree is None else _top_level_functions(tree)

    def _fixed_names(self, mod: str) -> frozenset[str] | None:
        try:
            text = self._path(mod).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        if _FIXED_CTOR_LOOSE.search(text) is None or _FIXED_CTOR_HINT.search(text) is None:
            return None
        tree = self._tree(mod)
        return (_fixed_instant_names(tree) or None) if tree is not None else None

    def _imports(self, mod: str, name: str) -> tuple[str, str] | None:
        tree = self.trees.get(mod)
        if tree is None:
            return None
        mod_parts = mod.split(".")
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                if node.module.startswith("."):
                    level = len(node.module) - len(node.module.lstrip("."))
                    base = mod_parts[: len(mod_parts) - (level - 1)]
                    cand = ".".join((*base, *node.module.lstrip(".").split(".")))
                else:
                    cand = node.module
                if cand in self.trees:
                    for alias in node.names:
                        if alias.name == name:
                            return cand, alias.name
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    leaf = alias.name.split(".")[-1]
                    if name in (alias.asname, leaf) and alias.name in self.trees:
                        return alias.name, leaf
        return None

    def _resolve(self, mod: str, call: ast.Call) -> tuple[str, str] | None:
        f = call.func
        if isinstance(f, ast.Name):
            if f.id in self.functions(mod):
                return mod, f.id
            return self._imports(mod, f.id)
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            direct = self._imports(mod, f.value.id)
            if direct is not None:
                if f.attr in self.functions(direct[0]):
                    return direct[0], f.attr
                sub = f"{direct[0]}.{direct[1]}"
                if f.attr in self.functions(sub):
                    return sub, f.attr
        return None

    def _inside_fallback(
        self, fn_node: ast.AST, target: ast.AST, clock_params: frozenset[str]
    ) -> bool:
        ancestry: list[ast.AST] = []

        def walk(node: ast.AST) -> bool:
            if node is target:
                return True
            for child in ast.iter_child_nodes(node):
                if walk(child):
                    ancestry.append(node)
                    return True
            return False

        walk(fn_node)
        for anc in ancestry:
            if isinstance(anc, ast.If | ast.IfExp):
                tests: list[ast.AST] = [anc.test]
            elif isinstance(anc, ast.BoolOp):
                tests = list(anc.values)
            else:
                continue
            for test in tests:
                if any(isinstance(n, ast.Name) and n.id in clock_params for n in ast.walk(test)):
                    return True
        return False

    def _call_threaded(
        self, call: ast.Call, callee: tuple[str, str], caller_clock: frozenset[str]
    ) -> bool:
        mod, name = callee
        info = self.functions(mod).get(name)
        if info is None:
            return False
        node, params = info
        cclock = params & CLOCK_PARAMS
        if not cclock:
            return False
        all_params = [p.arg for p in node.args.posonlyargs + node.args.args]
        supplied: set[str] = set()
        for i in range(min(len(call.args), len(all_params))):
            if all_params[i] in cclock:
                supplied.add(all_params[i])
        for kw in call.keywords:
            if kw.arg in cclock:
                supplied.add(kw.arg)
        if not cclock <= supplied:
            return False

        def pin_safe(expr: ast.AST) -> bool:
            for n in ast.walk(expr):
                if isinstance(n, ast.Name) and n.id in caller_clock:
                    return True
                if is_real_now_call(n):
                    return False
            return True

        for i in range(min(len(call.args), len(all_params))):
            if all_params[i] in cclock and not pin_safe(call.args[i]):
                return False
        return all(pin_safe(kw.value) for kw in call.keywords if kw.arg in cclock)

    def _walk_summary(
        self,
        mod: str,
        node: ast.AST,
        clock: frozenset[str],
        visited: set[tuple[str, str]],
        depth: int,
    ) -> tuple[bool, bool, bool]:
        uses = False
        covered = True
        family = mod in self.fixed
        for sub in ast.walk(node):
            if is_real_now_call(sub):
                uses = True
                covered = covered and bool(clock) and self._inside_fallback(node, sub, clock)
            if isinstance(sub, ast.Call):
                resolved = self._resolve(mod, sub)
                if resolved is None:
                    continue
                cuses, ccovered, cfam = self.summary(*resolved, depth + 1, frozenset(visited))
                if not cuses:
                    continue
                uses = True
                family = family or cfam
                if not self._inside_fallback(node, sub, clock) and not (
                    ccovered and self._call_threaded(sub, resolved, clock)
                ):
                    covered = False
        return uses, covered, family

    def summary(
        self, mod: str, name: str, depth: int = 0, seen: frozenset | None = None
    ) -> tuple[bool, bool, bool]:
        key = (mod, name)
        cached = self._summary.get(key)
        if cached is not None:
            return cached
        info = self.functions(mod).get(name)
        if info is None:
            return (False, True, False)
        node, params = info
        clock = params & CLOCK_PARAMS
        visited: set[tuple[str, str]] = set(seen) if seen is not None else set()
        if key in visited:
            return (False, True, False)
        visited = visited | {key}
        uses, covered, family = self._walk_summary(mod, node, clock, visited, depth)
        self._summary[key] = (uses, covered, family)
        return (uses, covered, family)
