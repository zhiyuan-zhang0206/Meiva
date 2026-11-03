from __future__ import annotations

import pytest

from services.supervision.roster.field_values import (
    flag,
    optional_text,
    real,
    text,
    texts,
    whole,
)


def test_readers_accept_the_declared_type() -> None:
    assert text("a", "s") == "a"
    assert optional_text(None, "s") is None
    assert optional_text("a", "s") == "a"
    assert flag(True, "s") is True
    assert whole(7, "s") == 7
    assert real(2, "s") == 2.0
    assert real(2.5, "s") == 2.5
    assert texts(["a", "b"], "s") == ("a", "b")
    assert texts(("a",), "s") == ("a",)


@pytest.mark.parametrize(
    ("reader", "value"),
    [
        (text, 1),
        (optional_text, 1),
        (flag, 1),
        (whole, True),
        (whole, 1.5),
        (whole, "7"),
        (real, True),
        (real, "1.0"),
        (texts, "ab"),
        (texts, ["a", 1]),
        (texts, ["a", None]),
        (texts, None),
    ],
)
def test_readers_refuse_another_type_naming_the_setting(reader: object, value: object) -> None:
    assert callable(reader)
    with pytest.raises(TypeError, match="setting probe_setting"):
        reader(value, "probe_setting")
