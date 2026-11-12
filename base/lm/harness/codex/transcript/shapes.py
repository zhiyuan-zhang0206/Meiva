from __future__ import annotations

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.lm.harness.codex.transcript.errors import MissingFieldError

type Obj = dict[str, object]


def obj(value: object, field: str) -> Obj:
    if is_object_dict(value):
        return value
    raise MissingFieldError(field)


def objects(row: Obj, field: str) -> list[Obj]:
    value = row.get(field)
    if not is_object_list(value):
        raise MissingFieldError(field)
    found: list[Obj] = []
    for element in value:
        if not is_object_dict(element):
            raise MissingFieldError(field)
        found.append(element)
    return found


def opt_objects(row: Obj, field: str) -> list[Obj]:
    return objects(row, field) if row.get(field) is not None else []


def strings(row: Obj, field: str) -> list[str]:
    value = row.get(field)
    if not is_object_list(value):
        raise MissingFieldError(field)
    if not all(isinstance(element, str) for element in value):
        raise MissingFieldError(field)
    return [element for element in value if isinstance(element, str)]


def opt_strings(row: Obj, field: str) -> list[str]:
    return strings(row, field) if row.get(field) is not None else []
