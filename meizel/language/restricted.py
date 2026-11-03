"""Restricted Python forms: constructs whose first-party dependency a static scan cannot see."""

from __future__ import annotations

import ast
import re

from meizel.graph.imports import ImportAccess
from meizel.graph.model import Restricted
from meizel.graph.universe import Universe
from meizel.language.bindings import module_bindings

__all__ = ["DYNAMIC_ATTR_FORMS", "EXTERNAL_FORMS", "RestrictedForms", "const_str"]

_RESTRICTED_REFS = {
    "importlib.import_module": "importlib.import_module",
    "runpy.run_module": "runpy.run_module",
    "runpy.run_path": "runpy.run_path",
    "importlib.util.spec_from_file_location": "importlib.util.spec_from_file_location",
    "builtins.__import__": "__import__",
    "builtins.exec": "exec of source text",
    "builtins.compile": "compile of source text",
}
# The forms a file may use once `[external]` declares that it runs code from outside the repository.
EXTERNAL_FORMS = frozenset(
    {
        "import()",
        "require()",
        "importlib.import_module",
        "runpy.run_module",
        "runpy.run_path",
        "importlib.util.spec_from_file_location",
        "__import__",
        "exec of source text",
        "compile of source text",
    }
)
# meiva:note
# The forms a file may use once `[dynamic-attrs]` declares that it owns an attribute name that only
# the running program knows: a computed name given to `getattr`/`hasattr`/`setattr`/`delattr`, a
# module that answers for names it does not bind, and a class that forwards the attributes it lacks.
DYNAMIC_NAME = "dynamic attribute name"
MODULE_GETATTR = "module __getattr__"
CLASS_GETATTR = "class __getattr__"
DYNAMIC_ATTR_FORMS = frozenset({DYNAMIC_NAME, MODULE_GETATTR, CLASS_GETATTR})
_REEXPORT_HOPS = 6
_ATTRIBUTE_BUILTINS = frozenset({"getattr", "hasattr", "setattr", "delattr"})
_FORWARDING_METHODS = frozenset({"__getattr__", "__getattribute__"})
_BUILTIN_REFS = frozenset({"__import__", "exec", "compile"})
_SYS_MODULES_METHODS = frozenset({"setdefault", "pop", "update"})
_SHELL_CALLS = frozenset(
    {
        "subprocess.run",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.Popen",
        "os.system",
        "os.popen",
        "asyncio.create_subprocess_shell",
    }
)
_IMPORT_IN_SOURCE = re.compile(
    r"""(?:^|[;\n("'])[ \t]*(?:from|import)[ \t]+([A-Za-z_]\w*)""", re.MULTILINE
)
_SHELL_PYTHON_C = re.compile(r"\bpython[\d.]*\s+(?:-\S+\s+)*-c\s")


def const_str(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


class RestrictedForms(ImportAccess):
    """Records the restricted forms of one file next to its import-access facts."""

    def __init__(self, universe: Universe, path: str, tree: ast.Module, *, walrus: bool) -> None:
        super().__init__(universe, path, tree, walrus=walrus)
        self.restricted: list[Restricted] = []
        # meiva:note
        # The tracked files whose content this file's scan read besides its own: a cached scan is
        # stale when one of them changes.
        self.binding_files: set[str] = set()

    def restrict(self, line: int, form: str, detail: str = "") -> None:
        self.restricted.append(Restricted(line, form, detail))

    def visit_name(self, node: ast.Name) -> None:
        if not isinstance(node.ctx, ast.Load):
            return
        origin, bound = self.scope.find(node.id)
        if origin is not None:
            form = _RESTRICTED_REFS.get(origin)
            if form:
                self.restrict(node.lineno, form)
        elif not bound and node.id in _BUILTIN_REFS:
            self.restrict(node.lineno, _RESTRICTED_REFS[f"builtins.{node.id}"])

    def restricted_attribute(self, origin: str, attrs: list[str], line: int) -> None:
        parts = [origin, *attrs]
        for end in range(1, len(parts) + 1):
            form = _RESTRICTED_REFS.get(".".join(parts[:end]))
            if form:
                self.restrict(line, form)

    # meiva:note
    # `getattr`/`hasattr`/`setattr`/`delattr` of the builtins (through an alias too) whose name is
    # not a string the source states: a literal, or a name bound once to one, here or in a first-party
    # module. Which attribute the call touches is then a run-time fact, not one the checker can read.
    # The builtin may also be handed to another call as an argument (`object_of(getattr, obj, name)`):
    # the arguments after it are then the object and the name, and the call is judged the same way.
    def attribute_name_call(self, node: ast.Call) -> None:
        if self.is_attribute_builtin(node.func):
            rest = node.args
        else:
            handed = [
                i
                for i, arg in enumerate(node.args)
                if isinstance(arg, ast.Name | ast.Attribute) and self.is_attribute_builtin(arg)
            ]
            if not handed:
                return
            rest = node.args[handed[0] + 1 :]
        spread = any(isinstance(arg, ast.Starred) for arg in rest[:2])
        if spread or (len(rest) >= 2 and self.stated_name(rest[1]) is None):
            self.restrict(node.lineno, DYNAMIC_NAME)

    def is_attribute_builtin(self, func: ast.expr) -> bool:
        if isinstance(func, ast.Name):
            origin, bound = self.scope.find(func.id)
            if origin is None:
                return not bound and func.id in _ATTRIBUTE_BUILTINS
        else:
            origin = self.dotted(func)
        return (
            origin is not None
            and origin.startswith("builtins.")
            and origin.removeprefix("builtins.") in _ATTRIBUTE_BUILTINS
        )

    # meiva:note
    # The string an attribute-name argument states, or None when only the run knows it.
    def stated_name(self, node: ast.expr, depth: int = 0) -> str | None:
        if depth > 8:
            return None
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, str) else None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = self.stated_name(node.left, depth + 1)
            right = self.stated_name(node.right, depth + 1)
            return None if left is None or right is None else left + right
        if isinstance(node, ast.Name):
            value = self.scope.constant(node.id)
            if value is not None:
                return self.stated_name(value, depth + 1)
        if isinstance(node, ast.Name | ast.Attribute):
            origin = self.dotted(node)
            if origin is not None and self.u.is_first_party_name(origin):
                return self.module_string(origin)
        return None

    # meiva:note
    # The string a first-party module's top-level name is bound to, when the name is bound once to a
    # literal there; a re-export is followed for as many hops as a class lookup follows one.
    def module_string(self, dotted: str, hops: int = _REEXPORT_HOPS) -> str | None:
        owner, _, name = dotted.rpartition(".")
        module = self.u.resolve_module(owner) if hops and owner else None
        if module is None or module.file is None:
            return None
        read = module_bindings(self.u, module.file, seen=self.binding_files)
        value = dict(read.strings).get(name)
        if value is not None or name in read.defined:
            return value
        origin = dict(read.reexports).get(name)
        return None if origin is None or origin == dotted else self.module_string(origin, hops - 1)

    # meiva:note
    # `__getattr__` at module level, and `__getattr__`/`__getattribute__` in a class body.
    def forwarding_method(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, *, depth: int
    ) -> None:
        if self.scope.is_class:
            if node.name in _FORWARDING_METHODS:
                self.restrict(node.lineno, CLASS_GETATTR)
        elif depth == 0 and node.name == "__getattr__":
            self.restrict(node.lineno, MODULE_GETATTR)

    # --- sys.modules ---------------------------------------------------------------

    def is_sys_modules(self, expr: ast.expr) -> bool:
        return self.dotted(expr) == "sys.modules"

    def is_sys_modules_method(self, func: ast.expr) -> bool:
        return (
            isinstance(func, ast.Attribute)
            and func.attr in _SYS_MODULES_METHODS
            and self.is_sys_modules(func.value)
        )

    def fake_module(self, key: ast.expr, line: int) -> None:
        """Writing a first-party module into `sys.modules` by a literal name. Reads are fine."""
        text = const_str(key)
        if text is not None and self.u.is_first_party_name(text):
            self.restrict(line, "sys.modules write of a first-party module", text)

    def sys_modules_call(self, node: ast.Call) -> None:
        if not node.args:
            for keyword in node.keywords:
                self.fake_module(ast.Constant(keyword.arg), node.lineno)
            return
        first = node.args[0]
        if isinstance(first, ast.Dict):
            for key in first.keys:
                if key is not None:
                    self.fake_module(key, node.lineno)
        else:
            self.fake_module(first, node.lineno)

    def sys_modules_write(self, node: ast.Subscript) -> None:
        if isinstance(node.ctx, ast.Store | ast.Del) and self.is_sys_modules(node.value):
            self.fake_module(node.slice, node.lineno)

    def patched_sys_modules(self, node: ast.Call) -> None:
        """`patch.dict(sys.modules, {...})` (or the string form) with first-party keys."""
        first = node.args[0] if node.args else None
        if first is None or not (self.is_sys_modules(first) or const_str(first) == "sys.modules"):
            return
        values = node.args[1] if len(node.args) > 1 else None
        if isinstance(values, ast.Dict):
            for key in values.keys:
                if key is not None:
                    self.fake_module(key, node.lineno)
        for keyword in node.keywords:
            self.fake_module(ast.Constant(keyword.arg), node.lineno)

    @staticmethod
    def is_shell_call(callee: str | None) -> bool:
        return callee in _SHELL_CALLS

    def shell_call(self, node: ast.Call) -> None:
        text = const_str(node.args[0]) if node.args else None
        if text is not None and _SHELL_PYTHON_C.search(text):
            self.check_source(text, node.lineno)

    def check_source(self, text: str, line: int) -> None:
        if any(self.u.is_first_party_name(m) for m in _IMPORT_IN_SOURCE.findall(text)):
            self.restrict(line, "python -c source that imports first-party code")

    def string_value(self, node: ast.expr, depth: int = 0) -> str | None:
        if depth > 8:
            return None
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, str) else None
        if isinstance(node, ast.JoinedStr):
            return self.joined_value(node)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = self.string_value(node.left, depth + 1)
            right = self.string_value(node.right, depth + 1)
            return None if left is None or right is None else left + right
        if isinstance(node, ast.Name):
            value = self.scope.constant(node.id)
            return None if value is None else self.string_value(value, depth + 1)
        return None

    @staticmethod
    def joined_value(node: ast.JoinedStr) -> str | None:
        """An f-string's literal text, with each interpolation reduced to `{}`."""
        parts = ["{}" if isinstance(v, ast.FormattedValue) else const_str(v) for v in node.values]
        return None if None in parts else "".join(p for p in parts if p is not None)
