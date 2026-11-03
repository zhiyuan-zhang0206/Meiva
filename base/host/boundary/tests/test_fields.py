# meiva:note
# `base.host.boundary.fields`: required and optional typed reads from an untyped mapping.

from __future__ import annotations

import pytest

from base.host.boundary.fields import (
    bool_field,
    int_field,
    optional_bool_field,
    optional_float_field,
    optional_int_field,
    optional_list_field,
    optional_object_field,
    optional_str_field,
    str_field,
)

_ROW: dict[str, object] = {
    "n": 3,
    "s": "x",
    "b": True,
    "f": 1.5,
    "o": {"k": 1},
    "l": [1],
    "none": None,
}


def test_required_fields_read_a_value_of_the_declared_type() -> None:
    assert int_field(_ROW, "n") == 3
    assert str_field(_ROW, "s") == "x"
    assert bool_field(_ROW, "b") is True


@pytest.mark.parametrize(
    ("read", "key", "message"),
    [
        (int_field, "b", "b is bool, not int"),
        (int_field, "s", "s is str, not int"),
        (str_field, "n", "n is int, not str"),
        (bool_field, "n", "n is int, not bool"),
        (int_field, "absent", "absent is missing, not int"),
        (str_field, "none", "none is NoneType, not str"),
    ],
)
def test_a_required_field_of_another_type_or_absent_is_a_type_error(
    read: object, key: str, message: str
) -> None:
    assert callable(read)
    with pytest.raises(TypeError, match=message):
        read(_ROW, key)


def test_optional_fields_answer_none_instead_of_raising() -> None:
    assert optional_int_field(_ROW, "n") == 3
    assert optional_int_field(_ROW, "b") is None
    assert optional_str_field(_ROW, "s") == "x"
    assert optional_str_field(_ROW, "absent") is None
    assert optional_bool_field(_ROW, "b") is True
    assert optional_bool_field(_ROW, "n") is None
    assert optional_float_field(_ROW, "f") == 1.5
    assert optional_float_field(_ROW, "n") == 3.0
    assert optional_float_field(_ROW, "b") is None
    assert optional_object_field(_ROW, "o") == {"k": 1}
    assert optional_object_field(_ROW, "l") is None
    assert optional_list_field(_ROW, "l") == [1]
    assert optional_list_field(_ROW, "o") is None
