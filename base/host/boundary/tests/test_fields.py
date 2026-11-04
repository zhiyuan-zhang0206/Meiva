# meiva:note
# `base.host.boundary.fields`: required and optional typed reads from an untyped mapping.

from __future__ import annotations

from collections.abc import Callable, Mapping

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
    typed_optional_bool_field,
    typed_optional_float_field,
    typed_optional_int_field,
    typed_optional_list_field,
    typed_optional_object_field,
    typed_optional_str_field,
)

_ROW: dict[str, object] = {
    "n": 3,
    "s": "x",
    "b": True,
    "f": 1.5,
    "o": {"k": 1},
    "l": [1],
    "none": None,
    "empty_text": "",
    "empty_object": {},
    "empty_list": [],
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


@pytest.mark.parametrize(
    ("read", "key", "expected"),
    [
        (typed_optional_str_field, "s", "x"),
        (typed_optional_int_field, "n", 3),
        (typed_optional_float_field, "f", 1.5),
        (typed_optional_float_field, "n", 3.0),
        (typed_optional_object_field, "o", {"k": 1}),
        (typed_optional_list_field, "l", [1]),
        (typed_optional_bool_field, "b", True),
    ],
)
def test_typed_optional_fields_read_a_value_of_the_declared_type(
    read: Callable[[Mapping[str, object], str], object], key: str, expected: object
) -> None:
    assert read(_ROW, key) == expected


@pytest.mark.parametrize(
    "read",
    [
        typed_optional_str_field,
        typed_optional_int_field,
        typed_optional_float_field,
        typed_optional_object_field,
        typed_optional_list_field,
        typed_optional_bool_field,
    ],
)
@pytest.mark.parametrize("key", ["absent", "none"])
def test_typed_optional_fields_read_an_absent_or_null_key_as_none(
    read: Callable[[Mapping[str, object], str], object], key: str
) -> None:
    assert read(_ROW, key) is None


@pytest.mark.parametrize(
    ("read", "key", "message"),
    [
        (typed_optional_str_field, "n", "n is int, not str"),
        (typed_optional_int_field, "b", "b is bool, not int"),
        (typed_optional_int_field, "s", "s is str, not int"),
        (typed_optional_float_field, "b", "b is bool, not a number"),
        (typed_optional_float_field, "s", "s is str, not a number"),
        (typed_optional_object_field, "l", "l is list, not an object"),
        (typed_optional_list_field, "o", "o is dict, not a list"),
        (typed_optional_bool_field, "n", "n is int, not bool"),
        (typed_optional_bool_field, "s", "s is str, not bool"),
    ],
)
def test_typed_optional_fields_refuse_another_type_instead_of_reading_it_as_absent(
    read: Callable[[Mapping[str, object], str], object], key: str, message: str
) -> None:
    with pytest.raises(TypeError, match=message):
        read(_ROW, key)


@pytest.mark.parametrize(
    ("read", "key", "expected"),
    [
        (typed_optional_str_field, "empty_text", ""),
        (typed_optional_object_field, "empty_object", {}),
        (typed_optional_list_field, "empty_list", []),
    ],
)
def test_typed_optional_fields_read_an_empty_value_as_that_value(
    read: Callable[[Mapping[str, object], str], object], key: str, expected: object
) -> None:
    # meiva:note
    # Only an absent or null key is "none": a call site's `record[key] or default` must keep an empty
    # string, object or array, so a reader answering None for a falsy value would turn a recorded
    # empty answer into "not recorded" without any error.
    assert read(_ROW, key) == expected


def test_the_two_optional_families_differ_only_on_a_value_of_another_type() -> None:
    assert optional_str_field(_ROW, "n") is None
    with pytest.raises(TypeError, match="n is int, not str"):
        typed_optional_str_field(_ROW, "n")
