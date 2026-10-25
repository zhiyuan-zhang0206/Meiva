# meiva:note
# `meiva.const()` — opt-in documented constant values.
#
# The wrapper factory, split out of `meiva/__init__.py`. The package entry imports `const`
# BEFORE its submodule imports, so top-level const assignments (e.g. `meiva.self.AGENT_ID =
# meiva.const(...)`) keep working during submodule load.


import inspect
from types import ModuleType, SimpleNamespace
from typing import Any, TypeGuard

# meiva:note
# Class attribute set on every type `const()` mints; `is_documented_const` reads it.
_DOCUMENTED_MARKER = "__meiva_documented_const__"


# meiva:note
# True if `obj`'s type is one minted by `const()`.
def is_documented_const(obj: object) -> bool:
    return vars(type(obj)).get(_DOCUMENTED_MARKER) is True


def is_container(obj: object) -> bool:
    return inspect.ismodule(obj) or inspect.isclass(obj) or isinstance(obj, SimpleNamespace)


def is_skill_object(obj: object) -> bool:
    return hasattr(obj, "_meiva_skill_kind")


def is_namespace(obj: object) -> TypeGuard[ModuleType | SimpleNamespace]:
    return isinstance(obj, (ModuleType, SimpleNamespace))


def format_docstring(doc: str, *, indent: str) -> str:
    triple = '"""' if '"""' not in doc else "'''"
    lines = doc.split("\n")
    if len(lines) == 1:
        return f"{indent}{triple}{lines[0]}{triple}"
    indented_body = "\n".join(f"{indent}{line}" if line else "" for line in lines[1:])
    return f"{indent}{triple}{lines[0]}\n{indented_body}\n{indent}{triple}"


# meiva:note
# Wrap a constant value with a docstring so `help(value)` shows the doc.
#
# Returns a `type(value)` subclass instance carrying `__doc__` set to `doc`. Use this for static
# module-level constants where the agent might pass the value directly to `help()` and expect
# documentation rather than just the runtime repr.
#
# The wrapper preserves the value's behavior: arithmetic, comparison, `isinstance` checks, and
# stringification all work as if it were the bare value. Only constants whose type is
# subclassable benefit — built-ins like `int`, `str`, `tuple`, `frozenset`, `Path`, `float` all
# work; `bool` and `NoneType` cannot be subclassed and will raise `TypeError`.
#
# For constants that mutate at runtime (e.g. `meiva.self.AGENT_ID`), prefer PEP 224 attribute
# docstrings instead — `help(meiva)` lists them via AST scan and docs survive in-place
# reassignment.
#
# Args: value: the constant value. doc: short natural-language description, surfaced as the H1
# body when `help(wrapped)` is called.
#
# Returns: A subclass instance of `type(value)` with `__doc__ == doc`. The instance is `==`,
# `is`-comparable (within Python's interning rules), and `isinstance(x, type(value))` returns
# `True`.
#
# Raises: TypeError: `type(value)` is not subclassable.
def const[T](value: T, doc: str) -> T:
    base: Any = type(value)
    cls = type(
        f"Documented{base.__name__}",
        (base,),
        {
            "__module__": __name__,
            "__qualname__": f"const.Documented{base.__name__}",
            _DOCUMENTED_MARKER: True,
        },
    )
    instance: Any = cls(value)
    instance.__doc__ = doc
    return instance
