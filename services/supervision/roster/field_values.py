# meiva:note
# A setting read by name arrives as an untyped value; each reader here checks the type the setting
# declares, so a root that composes services holds an `int` or a `str`, not an unchecked value. A
# bool is not a number, and a value of another type is a `TypeError` naming the setting.

from __future__ import annotations

from typing import TypeGuard


def _wrong(name: str, value: object, wanted: str) -> TypeError:
    return TypeError(f"setting {name} is {type(value).__name__}, not {wanted}")


def text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise _wrong(name, value, "str")
    return value


def optional_text(value: object, name: str) -> str | None:
    return None if value is None else text(value, name)


def flag(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise _wrong(name, value, "bool")
    return value


def whole(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _wrong(name, value, "int")
    return value


def real(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _wrong(name, value, "float")
    return float(value)


def _is_sequence(value: object) -> TypeGuard[list[object] | tuple[object, ...]]:
    return isinstance(value, list | tuple)


def texts(value: object, name: str) -> tuple[str, ...]:
    if not _is_sequence(value):
        raise _wrong(name, value, "a list of str")
    names: list[str] = []
    for item in value:
        names.append(text(item, name))
    return tuple(names)
