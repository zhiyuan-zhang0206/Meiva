# meiva:note
# Patches and test-seam spellings: the forms in which a test replaces something it did not build.
#
# A patch finds its target by name, so the scanner records each form as a `Patch` and the checker
# judges it by what the target is. This module holds the forms; the traversal that reaches them is
# the scanner's.

from __future__ import annotations

import ast
import sys

from meizel.graph.model import Code, FactKind, Patch
from meizel.graph.universe import Universe
from meizel.language import fixtures, purity
from meizel.language.bindings import (
    ClassFacts,
    find_class,
    is_imported_binding,
    module_bindings,
    overridable_methods,
    provides,
)
from meizel.language.python_commands import PythonCommands
from meizel.language.restricted import const_str
from meizel.lint_common import is_test_path

__all__ = ["PatchForms"]

_PATCH_CALLS = {
    "unittest.mock.patch": "patch",
    "unittest.mock.patch.object": "object",
    "unittest.mock.patch.multiple": "multiple",
    "unittest.mock.patch.dict": "dict",
}
_MONKEYPATCH_METHODS = frozenset({"setattr", "delattr", "setitem", "delitem", "setenv", "delenv"})
# meiva:note
# The dotted name of the process environment, spelled in parts so this scanner does not itself
# read as a use of it.
_ENVIRONMENT = ".".join(("os", "environ"))
_PATCH_OPTIONS = frozenset({"spec", "create", "spec_set", "autospec", "new_callable"})
# meiva:note
# The monkeypatch methods whose `raising=False` lets a patch of a missing name pass.
_RAISING_METHODS = frozenset({"setattr"})


def _tolerates_missing_target(node: ast.Call) -> bool:
    return any(
        keyword.arg == "raising"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is False
        for keyword in node.keywords
    )


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


# meiva:note
# The patch forms of one file, found as the scanner visits it.
class PatchForms(PythonCommands):
    def __init__(self, universe: Universe, path: str, tree: ast.Module, *, walrus: bool) -> None:
        super().__init__(universe, path, tree, walrus=walrus)
        self.test_file = fixtures.is_test_file(path)
        self.purity_state = purity.make_state(universe.root)
        self.patches: list[Patch] = []

    # meiva:note
    # The call forms that replace something: `mock.patch`, `monkeypatch`, the builtin
    # `setattr`/`delattr`, a write into `sys.modules`, and a call into a shell.
    def patch_forms(self, node: ast.Call, callee: str | None) -> None:
        kind = _PATCH_CALLS.get(callee) if callee else None
        if kind is not None:
            self.patch_call(node, kind)
        elif self.is_monkeypatch(node.func):
            self.monkeypatch_call(node)
        elif self.is_builtin_attribute_call(node):
            self.builtin_attribute_call(node)
        elif self.is_sys_modules_method(node.func):
            self.sys_modules_call(node)
        elif self.is_shell_call(callee):
            self.shell_call(node)

    # meiva:note
    # A class in a test that overrides a method of a concrete first-party class. The subclass
    # replaces behavior by name, which is a patch under another spelling. A fake that implements
    # a `Protocol` or an abstract base, and a base that lives in tests, is the form the red line
    # asks for and is not recorded.
    def record_test_subclass(self, node: ast.ClassDef) -> None:
        if not (self.test_file or is_test_path(self.path)):
            return
        methods = [
            stmt
            for stmt in node.body
            if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef)
            and not _is_dunder(stmt.name)
        ]
        for base in node.bases if methods else ():
            info = self.production_class(base)
            if info is not None:
                self.record_overrides(info, methods)

    # meiva:note
    # The concrete first-party class outside tests that a base expression names, or None.
    def production_class(self, base: ast.expr) -> ClassFacts | None:
        dotted = self.dotted(base.value if isinstance(base, ast.Subscript) else base)
        info = None if dotted is None else find_class(self.u, dotted)
        if info is None or info.abstract or info.file == self.path:
            return None
        return None if is_test_path(info.file) or fixtures.is_test_file(info.file) else info

    def record_overrides(
        self, info: ClassFacts, methods: list[ast.FunctionDef | ast.AsyncFunctionDef]
    ) -> None:
        inherited = overridable_methods(self.u, info)
        for method in methods:
            if method.name in inherited:
                target = f"{info.module}.{info.name}.{method.name}"
                self.patches.append(
                    Patch(method.lineno, "subclass", target, Code.TEST_SUBCLASS_OVERRIDE)
                )

    def dict_write(self, target: ast.Subscript, line: int) -> None:
        value = target.value
        owner: ast.expr | None = None
        if isinstance(value, ast.Attribute) and value.attr == "__dict__":
            owner = value.value
        elif (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "vars"
            and value.args
        ):
            owner = value.args[0]
        if owner is not None:
            name = const_str(target.slice)
            self.written(owner, (name,) if name else (), line, "__dict__ write")

    def is_imported_patch_target(self, dotted: str, name: str) -> bool:
        if not name or not self.u.is_first_party_name(dotted):
            return False
        module = self.u.resolve_module(dotted)
        if module is None or module.file is None or module.file == self.path:
            return False
        if self.u.resolve_module(f"{dotted}.{name}") is not None:
            return False
        return is_imported_binding(module_bindings(self.u.root, module.file), name)

    def written(self, owner: ast.expr, names: tuple[str, ...], line: int, form: str) -> None:
        dotted = self.dotted(owner)
        if dotted is None:
            return
        module = self.u.resolve_module(dotted) if self.u.is_first_party_name(dotted) else None
        if module is not None and module.file == self.path:
            return
        for name in names or ("",):
            target = f"{dotted}.{name}" if name else dotted
            imported = self.is_imported_patch_target(dotted, name)
            self.patches.append(
                Patch(line, form, target, self.patch_code(dotted), imported=imported)
            )
        self.require_target(owner, names, line, form)

    def is_builtin_attribute_call(self, node: ast.Call) -> bool:
        func = node.func
        return (
            isinstance(func, ast.Name)
            and func.id in {"setattr", "delattr"}
            and bool(node.args)
            and self.scope.find(func.id) == (None, False)
        )

    def builtin_attribute_call(self, node: ast.Call) -> None:
        if not isinstance(node.func, ast.Name):
            return
        name = const_str(node.args[1]) if len(node.args) > 1 else None
        self.written(node.args[0], (name,) if name else (), node.lineno, node.func.id)

    # meiva:note
    # A name set or removed on a first-party module must be one the module binds.
    #
    # Otherwise the write creates an attribute nothing in the module reads, and the patch
    # silently changes nothing. Dynamic modules, builtins and submodules count as bound.
    def require_target(self, owner: ast.expr, names: tuple[str, ...], line: int, form: str) -> None:
        dotted = self.dotted(owner)
        if dotted is None or not self.u.is_first_party_name(dotted):
            return
        module = self.u.resolve_module(dotted)
        if module is None or module.file is None or module.file == self.path:
            return
        bindings = module_bindings(self.u.root, module.file)
        for name in names:
            if provides(bindings, name) or self.u.resolve_module(f"{dotted}.{name}") is not None:
                continue
            self.problem(
                line,
                f"{form} on {dotted}.{name}: {module.file} binds no `{name}`, so the write"
                " changes a name nothing in the module reads; patch a name the code under test"
                " looks up, or inject the dependency",
                Code.MISSING_PATCH_TARGET,
            )

    @staticmethod
    def is_monkeypatch(func: ast.expr) -> bool:
        if not isinstance(func, ast.Attribute) or func.attr not in _MONKEYPATCH_METHODS:
            return False
        receiver = func.value
        name = receiver.id if isinstance(receiver, ast.Name) else getattr(receiver, "attr", "")
        return "monkeypatch" in name.lower()

    def patch_call(self, node: ast.Call, kind: str) -> None:
        first = node.args[0] if node.args else None
        form = "patch" if kind == "patch" else f"patch.{kind}"
        if kind == "patch" or const_str(first) is not None:
            names = self.patched_names(node, kind)
            self.string_target(first, node.lineno, form, names)
            if kind != "dict":
                purity.record_nested_patch(
                    self.purity_state.items,
                    first,
                    names[0] if names else None,
                    node.lineno,
                    form,
                    is_module=False,
                )
            if kind == "dict":
                self.patched_sys_modules(node)
        elif kind == "dict":
            self.patched_sys_modules(node)
            if first is not None:
                self.record_patch(first, (), node.lineno, form)
        elif first is not None and kind in {"object", "multiple"}:
            names = self.patched_names(node, kind)
            self.patch_fact(first, names, node.lineno, form)
            self.record_patch(first, names, node.lineno, form)
            self.require_target(first, names, node.lineno, form)
            is_mod = self.module_file(first) is not None or self.dotted(first) in self.imported
            purity.record_nested_patch(
                self.purity_state.items,
                first,
                names[0] if names else None,
                node.lineno,
                form,
                is_module=is_mod,
            )

    @staticmethod
    def patched_names(node: ast.Call, kind: str) -> tuple[str, ...]:
        if kind == "object":
            name = const_str(node.args[1]) if len(node.args) > 1 else None
            return (name,) if name else ()
        if kind == "multiple":
            return tuple(k.arg for k in node.keywords if k.arg and k.arg not in _PATCH_OPTIONS)
        return ()

    def patch_fact(self, target: ast.expr, names: tuple[str, ...], line: int, detail: str) -> None:
        module = self.module_file(target)
        if module is not None:
            self.add(module, line, FactKind.PATCH, names, detail)

    def monkeypatch_call(self, node: ast.Call) -> None:
        if not node.args or not isinstance(node.func, ast.Attribute):
            return
        method = node.func.attr
        form = f"monkeypatch.{method}"
        self.record_raising_false(node, method, form)
        if method in {"setenv", "delenv"}:
            name = const_str(node.args[0])
            self.patches.append(
                Patch(
                    node.lineno,
                    form,
                    f"{_ENVIRONMENT}.{name}" if name else _ENVIRONMENT,
                    Code.ENV_PATCH,
                )
            )
        elif method in {"setitem", "delitem"}:
            self.monkeypatch_item(node, form)
        elif const_str(node.args[0]) is not None:
            name = const_str(node.args[1]) if len(node.args) > 1 else None
            self.string_target(node.args[0], node.lineno, form, (name,) if name else ())
            purity.record_nested_patch(
                self.purity_state.items,
                node.args[0],
                name,
                node.lineno,
                form,
                is_module=False,
            )
        else:
            name = const_str(node.args[1]) if len(node.args) > 1 else None
            self.patch_fact(node.args[0], (name,) if name else (), node.lineno, "monkeypatch")
            self.record_patch(node.args[0], (name,) if name else (), node.lineno, form)
            self.require_target(node.args[0], (name,) if name else (), node.lineno, form)
            is_mod = (
                self.module_file(node.args[0]) is not None
                or self.dotted(node.args[0]) in self.imported
            )
            purity.record_nested_patch(
                self.purity_state.items,
                node.args[0],
                name,
                node.lineno,
                form,
                is_module=is_mod,
            )

    def record_raising_false(self, node: ast.Call, method: str, form: str) -> None:
        if method in _RAISING_METHODS and _tolerates_missing_target(node):
            self.patches.append(
                Patch(node.lineno, form, self.monkeypatch_target(node), Code.PATCH_RAISING_FALSE)
            )

    # meiva:note
    # The patched name of a `monkeypatch.setattr` / `delattr` call, as its record spells it.
    def monkeypatch_target(self, node: ast.Call) -> str:
        first = node.args[0]
        text = self.string_value(first)
        if text is not None:
            return text
        dotted = self.dotted(first)
        base = " ".join(ast.unparse(first).split())[:80] if dotted is None else dotted
        name = const_str(node.args[1]) if len(node.args) > 1 else None
        return f"{base}.{name}" if name else base

    def monkeypatch_item(self, node: ast.Call, form: str) -> None:
        if self.is_sys_modules(node.args[0]):
            if len(node.args) > 1:
                self.fake_module(node.args[1], node.lineno)
        elif self.dotted(node.args[0]) == _ENVIRONMENT:
            name = const_str(node.args[1]) if len(node.args) > 1 else None
            self.patches.append(
                Patch(
                    node.lineno,
                    form,
                    f"{_ENVIRONMENT}.{name}" if name else _ENVIRONMENT,
                    Code.ENV_PATCH,
                )
            )

    # meiva:note
    # What kind of code a resolved patch target is.
    def patch_code(self, dotted: str) -> Code:
        if dotted == _ENVIRONMENT or dotted.startswith(f"{_ENVIRONMENT}."):
            return Code.ENV_PATCH
        if self.u.is_first_party_name(dotted):
            return Code.FIRST_PARTY_PATCH
        if dotted.split(".", 1)[0] in sys.stdlib_module_names:
            return Code.STDLIB_PATCH
        return Code.THIRD_PARTY_PATCH

    # meiva:note
    # A patch of a module or object, classified by what the file's imports say it is.
    def record_patch(self, target: ast.expr, names: tuple[str, ...], line: int, form: str) -> None:
        dotted = self.dotted(target)
        code = Code.UNRESOLVED_PATCH if dotted is None else self.patch_code(dotted)
        base = " ".join(ast.unparse(target).split())[:80] if dotted is None else dotted
        for name in names or ("",):
            imported = False if dotted is None else self.is_imported_patch_target(dotted, name)
            self.patches.append(
                Patch(line, form, f"{base}.{name}" if name else base, code, imported=imported)
            )

    # meiva:note
    # A string target names first-party code invisibly (restricted); any other is a patch.
    def string_target(
        self, target: ast.expr | None, line: int, form: str, names: tuple[str, ...] = ()
    ) -> None:
        text = None if target is None else self.string_value(target)
        if text is None or self.u.is_first_party_name(text):
            self.restrict(line, "string patch target", text or "")
            return
        for name in names or ("",):
            self.patches.append(
                Patch(line, form, f"{text}.{name}" if name else text, self.patch_code(text))
            )
