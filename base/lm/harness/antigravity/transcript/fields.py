from __future__ import annotations

from collections.abc import Mapping

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.lm.harness.antigravity.transcript.errors import MissingError

# meiva:note
# The typed field readers of the Antigravity transcript parser. A field the projection needs that is
# absent or of the wrong type raises `MissingError`. A value the record may legitimately omit is read
# with an `optional_*` reader, which still raises when the field is present and malformed, so a changed
# shape is reported instead of read as "nothing".


def require_str(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MissingError(key)
    return value


def optional_str(row: Mapping[str, object], key: str) -> str | None:
    value = row.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise MissingError(key)
    return value


def optional_int(row: Mapping[str, object], key: str) -> int | None:
    value = row.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise MissingError(key)
    return value


def optional_list(row: Mapping[str, object], key: str) -> list[object]:
    value = row.get(key)
    if value is None:
        return []
    if not is_object_list(value):
        raise MissingError(key)
    return value


def require_step(row: Mapping[str, object]) -> int:
    value = row.get("step_index")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise MissingError("step_index")
    return value


def object_dict(value: object) -> dict[str, object]:
    return value if is_object_dict(value) else {}
