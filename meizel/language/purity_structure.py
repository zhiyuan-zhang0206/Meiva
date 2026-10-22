from __future__ import annotations

import ast

from meizel.graph.model import Code, EnumSubsetUse, PurityItem

__all__ = [
    "finalize_subsets",
    "record_attr_write",
    "record_enum_subsets",
    "record_nested_patch",
    "record_pass_through",
]

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


def _body_without_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


def record_pass_through(
    items: list[PurityItem],
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> None:
    if node.name.startswith(("_", "test_")) or node.decorator_list:
        return
    stmts = _body_without_docstring(node.body)
    if len(stmts) != 1 or not isinstance(stmts[0], ast.Return):
        return
    if not isinstance(stmts[0].value, ast.Call):
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


def finalize_subsets(
    subsets: list[EnumSubsetUse], defined_classes: set[str]
) -> list[EnumSubsetUse]:
    return [u for u in subsets if u.cls_name not in defined_classes]
