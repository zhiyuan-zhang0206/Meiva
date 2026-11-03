from typing import TypeGuard


# meiva:note
# A decoded JSON (or `jsonb`) object: a dict whose keys are all strings, with the values still
# `object` until a reader narrows them. `json.loads`, psycopg's `jsonb`
# adapter and a request's raw body all hand back `Any`; this is the one place that claim is
# checked, so a reader holds `dict[str, object]` instead.
def is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return _is_dict(value) and all(isinstance(key, str) for key in value)


def _is_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


# meiva:note
# A decoded JSON array whose elements are still `object` until a reader narrows them.
def is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)
