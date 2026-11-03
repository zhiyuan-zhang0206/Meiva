# meiva:note
# The one boundary from an argparse `Namespace` to typed values.
#
# `argparse.Namespace` attributes are `Any`: every `args.name` read hands untyped data to a
# handler. A handler instead declares a frozen dataclass naming the attributes it reads and
# calls `options` once; from there on it holds real types. A value the entry binds beside the
# parsed flags (the command runtime, a database factory) is read with `attribute`.
#
# Nothing is defaulted: an attribute the namespace lacks, or one of the wrong type, is an
# `OptionsError` naming every such field at once. argparse has already checked what the operator
# typed, so this is a programming error (the shape and the parser disagree): it raises and is not
# reported as a usage error, and a mismatch fails the first time the verb runs.

from __future__ import annotations

import argparse
import collections.abc
import dataclasses
import types
import typing
from typing import TypeGuard

__all__ = ["OptionsError", "attribute", "options"]


# meiva:note
# A handler's declared options do not match what the namespace carries.
class OptionsError(Exception):
    pass


def _is_attribute_map(value: object) -> TypeGuard[collections.abc.Mapping[str, object]]:
    return _is_mapping(value) and all(isinstance(key, str) for key in value)


def _carried(args: argparse.Namespace) -> collections.abc.Mapping[str, object]:
    # meiva:note
    # The namespace's attributes as `object`: the seam where argparse's `Any` stops.
    carried: object = vars(args)
    if not _is_attribute_map(carried):
        raise OptionsError("the namespace carries a non-string attribute name")
    return carried


def _is_whole_number(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_tuple(value: object) -> TypeGuard[tuple[object, ...]]:
    return isinstance(value, tuple)


def _is_items(value: object) -> TypeGuard[collections.abc.Collection[object]]:
    return isinstance(value, collections.abc.Collection) and not isinstance(value, str | bytes)


def _is_mapping(value: object) -> TypeGuard[collections.abc.Mapping[object, object]]:
    return isinstance(value, collections.abc.Mapping)


def _is_callable_origin(origin: object) -> bool:
    return origin is typing.get_origin(collections.abc.Callable[..., object])


def _accepts_tuple(value: object, arguments: tuple[object, ...]) -> bool:
    if not _is_tuple(value):
        return False
    if len(arguments) == 2 and arguments[1] is Ellipsis:
        return all(_accepts(item, arguments[0]) for item in value)
    return len(value) == len(arguments) and all(
        _accepts(item, member) for item, member in zip(value, arguments, strict=True)
    )


# meiva:note
# A container annotation: a mapping checks keys and values, any other collection its elements.
# A `str` is a `Sequence[str]` whose items are `str`; a flag answered with one string where a list
# was declared is a parser bug, not a one-element list, so `_is_items` refuses `str` and `bytes`.
def _accepts_container(value: object, origin: type, arguments: tuple[object, ...]) -> bool:
    if issubclass(origin, collections.abc.Mapping):
        return (
            _is_mapping(value)
            and isinstance(value, origin)
            and all(
                _accepts(key, arguments[0]) and _accepts(item, arguments[1])
                for key, item in value.items()
            )
        )
    if issubclass(origin, collections.abc.Collection) and len(arguments) == 1:
        return (
            _is_items(value)
            and isinstance(value, origin)
            and all(_accepts(item, arguments[0]) for item in value)
        )
    return isinstance(value, origin)


def _accepts_generic(value: object, origin: object, arguments: tuple[object, ...]) -> bool:
    if origin is typing.Annotated:
        return _accepts(value, arguments[0])
    if origin is typing.Union or origin is types.UnionType:
        return any(_accepts(value, member) for member in arguments)
    if origin is typing.Literal:
        return any(type(value) is type(choice) and value == choice for choice in arguments)
    if _is_callable_origin(origin):
        return callable(value)
    if origin is tuple:
        return _accepts_tuple(value, arguments)
    return isinstance(origin, type) and _accepts_container(value, origin, arguments)


# meiva:note
# Whether `value` is an instance of the annotation `hint`, checked structurally down to the
# elements of containers. `object` accepts anything (an opaque value the handler narrows itself);
# `int` refuses a bool, because a flag answered `True` for a count is a parser bug. A parameter of
# a generic class that is not a container (`Popen[bytes]`) is not checked.
def _accepts(value: object, hint: object) -> bool:
    if hint is object:
        return True
    if hint is None or hint is type(None):
        return value is None
    origin: object = typing.get_origin(hint)
    if origin is not None:
        return _accepts_generic(value, origin, typing.get_args(hint))
    if not isinstance(hint, type):
        raise TypeError(f"unsupported option annotation {hint!r}")
    return _is_whole_number(value) if hint is int else isinstance(value, hint)


# meiva:note
# The attributes `shape` declares, read from `args` and checked against their annotations.
#
# `shape` is a dataclass: each field names a namespace attribute and its type. Attributes the
# dataclass does not declare are ignored.
def options[T](args: argparse.Namespace, shape: type[T]) -> T:
    if not dataclasses.is_dataclass(shape):
        raise TypeError(f"{shape.__name__} is not a dataclass")
    carried = _carried(args)
    hints: dict[str, object] = typing.get_type_hints(shape)
    values: dict[str, object] = {}
    problems: list[str] = []
    for field in dataclasses.fields(shape):
        if field.name not in carried:
            problems.append(f"{field.name} is missing")
            continue
        value = carried[field.name]
        if _accepts(value, hints[field.name]):
            values[field.name] = value
        else:
            problems.append(f"{field.name} is {type(value).__name__}, not {hints[field.name]}")
    if problems:
        raise OptionsError(f"{shape.__name__}: " + "; ".join(problems))
    return shape(**values)


# meiva:note
# The attribute `name` of `args`, which must be an instance of `kind`.
#
# For a value the process entry binds beside the parsed flags: it has a class of its own, so it
# needs no dataclass field.
def attribute[T](args: argparse.Namespace, name: str, kind: type[T]) -> T:
    carried = _carried(args)
    if name not in carried:
        raise OptionsError(f"{name} is missing")
    value = carried[name]
    if not isinstance(value, kind):
        raise OptionsError(f"{name} is {type(value).__name__}, not {kind.__name__}")
    return value
