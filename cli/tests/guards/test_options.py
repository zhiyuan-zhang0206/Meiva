# meiva:note
# `cli.boundary.options`: the boundary from an argparse namespace to typed values.

from __future__ import annotations

import argparse
import enum
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NewType

import pytest

from cli import main as _main
from cli.boundary.options import OptionsError, attribute, options
from cli.database import open_operator_database

_Id = NewType("_Id", int)


class _Mode(enum.Enum):
    FAST = "fast"


@dataclass(frozen=True)
class _Everything:
    name: str
    count: int
    ratio: float
    flag: bool
    where: Path | None
    names: list[str]
    pair: tuple[str, int]
    run: tuple[int, ...]
    mode: Literal["a", "b"]
    kind: _Mode
    table: dict[str, int] | None
    view: Mapping[str, Sequence[str]]
    hook: Callable[[int], int]
    opaque: object


def _namespace(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "name": "x",
        "count": 3,
        "ratio": 0.5,
        "flag": True,
        "where": None,
        "names": ["a", "b"],
        "pair": ("a", 1),
        "run": (1, 2),
        "mode": "a",
        "kind": _Mode.FAST,
        "table": {"k": 1},
        "view": {"k": ["v"]},
        "hook": abs,
        "opaque": 7,
        "unrelated": object(),
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_reads_every_declared_field_and_ignores_the_rest() -> None:
    opts = options(_namespace(where=Path("/somewhere")), _Everything)

    assert opts.name == "x"
    assert opts.where == Path("/somewhere")
    assert opts.kind is _Mode.FAST
    assert opts.names == ["a", "b"]
    assert not hasattr(opts, "unrelated")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", 1),
        ("count", True),
        ("count", "3"),
        ("ratio", 1),
        ("flag", 1),
        ("where", "/somewhere"),
        ("names", "ab"),
        ("names", ("a",)),
        ("names", ["a", 1]),
        ("pair", ("a",)),
        ("pair", (1, "a")),
        ("run", (1, "2")),
        ("mode", "c"),
        ("mode", None),
        ("kind", "fast"),
        ("table", {1: 1}),
        ("table", {"k": "v"}),
        ("view", {"k": "v"}),
        ("hook", 3),
    ],
)
def test_refuses_a_value_of_the_wrong_type(field: str, value: object) -> None:
    with pytest.raises(OptionsError, match=field):
        options(_namespace(**{field: value}), _Everything)


def test_names_every_problem_at_once() -> None:
    args = _namespace(name=1)
    delattr(args, "count")

    with pytest.raises(OptionsError) as raised:
        options(args, _Everything)

    message = str(raised.value)
    assert "_Everything" in message
    assert "name is int" in message
    assert "count is missing" in message


def test_nothing_is_defaulted() -> None:
    @dataclass(frozen=True)
    class Defaulted:
        name: str = "fallback"

    with pytest.raises(OptionsError, match="name is missing"):
        options(argparse.Namespace(), Defaulted)


def test_an_unset_optional_is_the_value_none_not_a_missing_field() -> None:
    @dataclass(frozen=True)
    class Optional:
        where: Path | None

    assert options(argparse.Namespace(where=None), Optional).where is None
    with pytest.raises(OptionsError, match="where is missing"):
        options(argparse.Namespace(), Optional)


def test_a_shape_that_is_not_a_dataclass_is_a_programming_error() -> None:
    with pytest.raises(TypeError, match="not a dataclass"):
        options(argparse.Namespace(), str)


def test_an_annotation_it_cannot_check_is_a_programming_error() -> None:
    @dataclass(frozen=True)
    class Unsupported:
        value: _Id

    with pytest.raises(TypeError, match="unsupported option annotation"):
        options(argparse.Namespace(value=3), Unsupported)


def test_attribute_returns_an_instance_of_the_named_class() -> None:
    assert attribute(argparse.Namespace(site=Path("/h")), "site", Path) == Path("/h")


def test_attribute_refuses_a_missing_or_foreign_value() -> None:
    with pytest.raises(OptionsError, match="site is missing"):
        attribute(argparse.Namespace(), "site", Path)
    with pytest.raises(OptionsError, match="site is str, not Path"):
        attribute(argparse.Namespace(site="/h"), "site", Path)


@dataclass(frozen=True)
class _Unbound:
    absent: str


# meiva:note
# A handler that declares an option its parser never produced raises before it acts, on the
# runtime path and on the init path alike: the mismatch is a programming error, not a usage error.
@pytest.mark.parametrize(
    "argv", [["status"], ["init", "--machine-name", "probe", "--serve-gateway"]]
)
def test_a_handler_the_namespace_does_not_fit_raises(argv: list[str], unit_home: Path) -> None:
    def _fake(args: argparse.Namespace) -> int:
        return options(args, _Unbound).absent.count("x")

    args = _main.parse_command_args(argv)
    args.func = _fake

    with pytest.raises(OptionsError, match="_Unbound: absent is missing"):
        _main.dispatch_parsed(
            args,
            argv,
            declared=unit_home,
            checkout=Path(_main.__file__).resolve().parents[1],
            producer_factory=_main.operator_producer,
            open_database=open_operator_database,
        )
