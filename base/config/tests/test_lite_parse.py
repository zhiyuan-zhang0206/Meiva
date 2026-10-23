# meiva:note
# The boot-lite parser mirrors pydantic's non-strict coercion for the table's field types. It is a
# pure function of one raw string, so each kind is checked here with values and no Settings.

from pathlib import Path

import pytest

from base.config.lite_parse import parse_field


def _parse(kind: str, raw: str, *, check: str | None = None) -> object:
    return parse_field("field", "MEIVA_FIELD", kind, check, raw)


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", "t", "y"])
def test_bool_true_spellings(raw: str) -> None:
    assert _parse("bool", raw) is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "f", "n"])
def test_bool_false_spellings(raw: str) -> None:
    assert _parse("bool", raw) is False


@pytest.mark.parametrize(
    ("raw", "expected"), [("5", 5), (" +7 ", 7), ("1_000", 1000), ("5.0", 5), ("-3", -3)]
)
def test_int_accepts_literals_and_integral_decimals(raw: str, expected: int) -> None:
    assert _parse("int", raw) == expected


@pytest.mark.parametrize("raw", ["5.", ".5", "5e1", "5.5", "", "x", "٣"])
def test_int_refuses_everything_pydantic_refuses(raw: str) -> None:
    with pytest.raises(ValueError, match="not a valid int"):
        _parse("int", raw)


def test_other_kinds() -> None:
    assert _parse("str", " kept ") == " kept "
    assert _parse("float", " 1.5 ") == 1.5
    assert _parse("path", "/srv/home") == Path("/srv/home")
    assert _parse("csv_str_list", " a, b ,,c ") == ["a", "b", "c"]


def test_a_bad_value_names_the_alias_and_the_field() -> None:
    with pytest.raises(ValueError, match=r"MEIVA_FIELD='maybe' is not a valid bool.*'field'"):
        _parse("bool", "maybe")


def test_an_unknown_kind_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown parse kind"):
        _parse("complex", "1")


def test_the_port_check_bounds_the_value() -> None:
    assert _parse("int", "4318", check="port") == 4318
    with pytest.raises(ValueError, match="between 1 and 65535"):
        _parse("int", "70000", check="port")


def test_the_timezone_check_names_the_zone() -> None:
    assert _parse("str", "Asia/Shanghai", check="iana") == "Asia/Shanghai"
    with pytest.raises(ValueError, match="not a valid IANA timezone"):
        _parse("str", "Not/A_Zone", check="iana")


def test_the_eval_allowlist_check_lists_unsupported_entries() -> None:
    assert _parse("csv_str_list", "web,understand", check="eval_allowlist") == ["web", "understand"]
    with pytest.raises(ValueError, match=r"unsupported entries: \['shell'\]"):
        _parse("csv_str_list", "web,shell", check="eval_allowlist")
