from __future__ import annotations

import ast
import sys

from meizel.graph.model import Code, EnumSubsetUse, PurityItem

__all__ = [
    "ALLOWED_STDLIB_MODULES",
    "ALL_KNOWN_STDLIB",
    "DEFAULT_IMPURE_IMPORTS",
    "IMPURE_DOTTED_STDLIB_MODULES",
    "IMPURE_STDLIB_MODULES",
    "record_attr_write",
    "record_enum_subsets",
    "record_module_structure",
    "record_nested_patch",
    "record_params",
    "record_pass_through",
]

IMPURE_STDLIB_MODULES: frozenset[str] = frozenset(
    {
        "_curses",
        "_curses_panel",
        "_ctypes",
        "_dbm",
        "_gdbm",
        "_lsprof",
        "_multiprocessing",
        "_posixsubprocess",
        "_signal",
        "_socket",
        "_sqlite3",
        "_ssl",
        "_tkinter",
        "_tracemalloc",
        "_winapi",
        "_wmi",
        "aifc",
        "antigravity",
        "audioop",
        "bdb",
        "cProfile",
        "compileall",
        "curses",
        "ctypes",
        "dbm",
        "ensurepip",
        "faulthandler",
        "fcntl",
        "ftplib",
        "grp",
        "idlelib",
        "imaplib",
        "lib2to3",
        "mmap",
        "msilib",
        "msvcrt",
        "multiprocessing",
        "netrc",
        "nis",
        "nntplib",
        "nt",
        "ossaudiodev",
        "pdb",
        "pipes",
        "poplib",
        "posix",
        "profile",
        "pstats",
        "pty",
        "pwd",
        "py_compile",
        "readline",
        "resource",
        "rlcompleter",
        "runpy",
        "select",
        "selectors",
        "shelve",
        "shutil",
        "signal",
        "smtplib",
        "sndhdr",
        "socket",
        "socketserver",
        "spwd",
        "sqlite3",
        "ssl",
        "subprocess",
        "sunau",
        "syslog",
        "telnetlib",
        "tempfile",
        "termios",
        "this",
        "tkinter",
        "trace",
        "tracemalloc",
        "tty",
        "turtle",
        "turtledemo",
        "venv",
        "wave",
        "webbrowser",
        "winreg",
        "winsound",
        "wsgiref",
        "xmlrpc",
        "zipapp",
    }
)

IMPURE_DOTTED_STDLIB_MODULES: frozenset[str] = frozenset(
    {
        "urllib.request",
        "urllib.robotparser",
        "urllib.response",
        "urllib.error",
        "http.client",
        "http.server",
        "http.cookiejar",
    }
)

ALL_KNOWN_STDLIB: frozenset[str] = frozenset(sys.stdlib_module_names | {"__future__", "builtins"})
ALLOWED_STDLIB_MODULES: frozenset[str] = frozenset(ALL_KNOWN_STDLIB - IMPURE_STDLIB_MODULES)

DEFAULT_IMPURE_IMPORTS: frozenset[str] = IMPURE_STDLIB_MODULES

_PASSTHROUGH_HINT = (
    "have callers invoke the target directly, or give this function a real boundary responsibility"
)
_NESTED_ATTR_WRITE_HINT = (
    "mutate state through a method on the owning object or return a new value instead of "
    "reaching through attributes"
)
_NESTED_PATCH_HINT = (
    "inject the collaborator directly instead of patching through an attribute chain"
)
_WIDE_PUBLIC_SURFACE_HINT = (
    "split the module into smaller cohesive modules or mark internal helpers private with `_`"
)
_WIDE_PARAMS_HINT = "group cohesive parameters into a `@dataclass(frozen=True)` value object"
_DEEP_ATTR_CHAIN_HINT = (
    "ask the immediate collaborator for the value via a method or pass only the needed slice"
)
_DATA_BASES = frozenset(
    {
        "Enum",
        "StrEnum",
        "IntEnum",
        "Flag",
        "IntFlag",
        "Protocol",
        "TypedDict",
        "NamedTuple",
        "BaseModel",
        "Exception",
        "RuntimeError",
        "ValueError",
        "KeyError",
        "LookupError",
        "TypeError",
    }
)


def _expected_params(args: ast.arguments) -> list[str] | None:
    if any(d is not None for d in (*args.defaults, *args.kw_defaults)):
        return None
    out = [a.arg for a in (*args.posonlyargs, *args.args)]
    if args.vararg is not None:
        out.append(f"*{args.vararg.arg}")
    out.extend(a.arg for a in args.kwonlyargs)
    if args.kwarg is not None:
        out.append(f"**{args.kwarg.arg}")
    return out


def _forwarded_args(call: ast.Call) -> list[str] | None:
    out: list[str] = []
    for arg in call.args:
        if isinstance(arg, ast.Name):
            out.append(arg.id)
        elif isinstance(arg, ast.Starred) and isinstance(arg.value, ast.Name):
            out.append(f"*{arg.value.id}")
        else:
            return None
    for kw in call.keywords:
        if kw.arg is None and isinstance(kw.value, ast.Name):
            out.append(f"**{kw.value.id}")
        elif kw.arg is not None and isinstance(kw.value, ast.Name) and kw.value.id == kw.arg:
            out.append(kw.arg)
        else:
            return None
    return out


def _body_without_docstring(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[ast.stmt]:
    if ast.get_docstring(node, clean=False) is not None:
        return node.body[1:]
    return node.body


def record_pass_through(
    items: list[PurityItem],
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> None:
    if node.name.startswith(("_", "test_")) or node.decorator_list:
        return
    stmts = _body_without_docstring(node)
    if len(stmts) != 1 or not isinstance(stmts[0], ast.Return):
        return
    if not isinstance(stmts[0].value, ast.Call):
        return
    func = stmts[0].value.func
    if not isinstance(func, ast.Name) and not (
        isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
    ):
        return
    expected = _expected_params(node.args)
    forwarded = _forwarded_args(stmts[0].value)
    if expected and forwarded == expected:
        items.append(
            PurityItem(
                node.lineno,
                Code.PASSTHROUGH_FUNCTION,
                node.name,
                f"public function `{node.name}` is a pass-through wrapper with no independent logic",
                _PASSTHROUGH_HINT,
            )
        )


def record_attr_write(items: list[PurityItem], target: ast.Attribute, line: int) -> None:
    if isinstance(target.value, ast.Attribute):
        chain = " ".join(ast.unparse(target).split())
        items.append(
            PurityItem(
                line,
                Code.NESTED_ATTR_WRITE,
                chain,
                f"nested attribute mutation `{chain}` reaches through an intermediate object",
                _NESTED_ATTR_WRITE_HINT,
            )
        )


def record_nested_patch(
    items: list[PurityItem],
    first: ast.expr | None,
    name: str | None,
    line: int,
    form: str,
    *,
    is_module: bool,
) -> None:
    if first is None:
        return
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        full = f"{first.value}.{name}" if name else first.value
        if full.count(".") > 1:
            items.append(
                PurityItem(
                    line,
                    Code.NESTED_PATCH_TARGET,
                    full,
                    f"{form} on nested target `{full}` reaches through an intermediate attribute",
                    _NESTED_PATCH_HINT,
                )
            )
    elif isinstance(first, ast.Attribute) and not is_module:
        base = " ".join(ast.unparse(first).split())
        full = f"{base}.{name}" if name else base
        items.append(
            PurityItem(
                line,
                Code.NESTED_PATCH_TARGET,
                full,
                f"{form} on nested target `{full}` reaches through an intermediate attribute",
                _NESTED_PATCH_HINT,
            )
        )


def _collection_elts(comp: ast.expr) -> list[ast.expr] | None:
    if isinstance(comp, ast.Tuple | ast.Set | ast.List):
        return comp.elts
    if (
        isinstance(comp, ast.Call)
        and isinstance(comp.func, ast.Name)
        and comp.func.id == "frozenset"
        and len(comp.args) == 1
        and isinstance(comp.args[0], ast.Tuple | ast.Set | ast.List)
    ):
        return comp.args[0].elts
    return None


def _enum_member_pair(expr: ast.expr) -> tuple[str, str] | None:
    if not isinstance(expr, ast.Attribute) or not expr.attr.isupper():
        return None
    if isinstance(expr.value, ast.Name):
        cls_name = expr.value.id
    elif isinstance(expr.value, ast.Attribute):
        cls_name = expr.value.attr
    else:
        return None
    if not cls_name or not cls_name[0].isupper() or cls_name.isupper() or "_" in cls_name:
        return None
    return cls_name, expr.attr


def record_enum_subsets(subsets: list[EnumSubsetUse], node: ast.Compare) -> None:
    for op, comp in zip(node.ops, node.comparators, strict=False):
        if not isinstance(op, ast.In | ast.NotIn):
            continue
        elts = _collection_elts(comp)
        if not elts:
            continue
        pairs = [_enum_member_pair(e) for e in elts]
        if any(p is None for p in pairs):
            continue
        cls_names = {p[0] for p in pairs if p is not None}
        members = tuple(sorted({p[1] for p in pairs if p is not None}))
        if len(cls_names) == 1 and len(members) >= 2:
            subsets.append(EnumSubsetUse(node.lineno, next(iter(cls_names)), members))


def _decorator_tail(expr: ast.expr) -> str:
    target = expr.func if isinstance(expr, ast.Call) else expr
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return ""


def _is_overload(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(_decorator_tail(d) == "overload" for d in node.decorator_list)


def record_params(
    items: list[PurityItem],
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    max_params: int,
) -> None:
    if _is_overload(node):
        return
    args = node.args
    params = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    if params and params[0].arg in {"self", "cls"}:
        params = params[1:]
    total = len(params) + (1 if args.vararg else 0) + (1 if args.kwarg else 0)
    if total > max_params:
        items.append(
            PurityItem(
                node.lineno,
                Code.WIDE_PARAMS,
                f"def {node.name}",
                f"function `{node.name}` has {total} parameters (max {max_params} in strict mode)",
                _WIDE_PARAMS_HINT,
            )
        )


def _base_tail(expr: ast.expr) -> str:
    target = expr.value if isinstance(expr, ast.Subscript) else expr
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return ""


def _is_frozen_dataclass(node: ast.ClassDef) -> bool:
    for deco in node.decorator_list:
        if (
            isinstance(deco, ast.Call)
            and _decorator_tail(deco) == "dataclass"
            and any(
                k.arg == "frozen" and isinstance(k.value, ast.Constant) and k.value.value is True
                for k in deco.keywords
            )
        ):
            return True
    return False


def _is_exempt_class(node: ast.ClassDef) -> bool:
    if _is_frozen_dataclass(node) or node.name.endswith(("Error", "Exception")):
        return True
    for base in node.bases:
        tail = _base_tail(base)
        if tail in _DATA_BASES or tail.endswith(("Error", "Exception")):
            return True
    return False


def _count_public_behavioral(tree: ast.Module) -> int:
    count = 0
    for stmt in tree.body:
        if (
            isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef)
            and not stmt.name.startswith("_")
            and not _is_overload(stmt)
        ) or (
            isinstance(stmt, ast.ClassDef)
            and not stmt.name.startswith("_")
            and not _is_exempt_class(stmt)
        ):
            count += 1
    return count


def _attr_parts(node: ast.Attribute) -> tuple[str, list[str]] | None:
    attrs: list[str] = []
    curr: ast.expr = node
    while isinstance(curr, ast.Attribute):
        attrs.append(curr.attr)
        curr = curr.value
    if not isinstance(curr, ast.Name):
        return None
    attrs.reverse()
    return curr.id, attrs


def _imported_module_prefixes(tree: ast.Module) -> set[str]:
    prefixes: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for alias in n.names:
                if alias.asname is None and "." in alias.name:
                    parts = alias.name.split(".")
                    for k in range(2, len(parts) + 1):
                        prefixes.add(".".join(parts[:k]))
    return prefixes


def _strip_module_hops(root_name: str, attrs: list[str], prefixes: set[str]) -> list[str]:
    parts = [root_name, *attrs]
    for k in range(len(parts), 1, -1):
        if ".".join(parts[:k]) in prefixes:
            return parts[k:]
    return attrs


def _record_deep_attr_chains(
    items: list[PurityItem],
    tree: ast.Module,
    max_attr_hops: int,
) -> None:
    prefixes = _imported_module_prefixes(tree)
    parent_values = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    call_funcs = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for n in ast.walk(tree):
        if (
            not isinstance(n, ast.Attribute)
            or not isinstance(n.ctx, ast.Load)
            or id(n) in parent_values
        ):
            continue
        chain = _attr_parts(n)
        if chain is None:
            continue
        root_name, attrs = chain
        eff = attrs[:-1] if id(n) in call_funcs else attrs
        eff = _strip_module_hops(root_name, eff, prefixes)
        if len(eff) > max_attr_hops:
            full = ".".join([root_name, *attrs])
            items.append(
                PurityItem(
                    n.lineno,
                    Code.DEEP_ATTR_CHAIN,
                    full,
                    f"attribute chain `{full}` has {len(eff)} hops (max {max_attr_hops} in strict mode)",
                    _DEEP_ATTR_CHAIN_HINT,
                )
            )


def record_module_structure(
    items: list[PurityItem],
    tree: ast.Module,
    max_public_symbols: int,
    max_attr_hops: int,
) -> None:
    public_count = _count_public_behavioral(tree)
    if public_count > max_public_symbols:
        items.append(
            PurityItem(
                1,
                Code.WIDE_PUBLIC_SURFACE,
                "public-surface",
                f"module defines {public_count} public behavioral symbols (max {max_public_symbols} in strict mode)",
                _WIDE_PUBLIC_SURFACE_HINT,
            )
        )
    _record_deep_attr_chains(items, tree, max_attr_hops)
