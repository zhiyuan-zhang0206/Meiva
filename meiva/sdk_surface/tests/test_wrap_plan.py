# meiva:note
# `meiva/sdk_surface/pure/plans/installation/wrap_plan.py` unit tests: the dotted-path rule of a
# wrap target and an SDK expansion, how the chained function presents itself, when a layer is
# async and when a firing is reported. Asserted by value; the installed wrap stack is covered by
# `meiva/tests/namespaces/sdk_surface/test_wraps.py`.

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable

import pytest

from meiva.sdk_surface.pure.plans.installation.wrap_plan import (
    activation_due,
    chained_face,
    is_public_dotted_path,
    layer_is_async,
    uncallable_message,
)


@pytest.mark.parametrize("path", ["files.read", "understand", "shell.sessions.new", "a1.b_c"])
def test_a_dotted_path_of_public_identifiers_is_accepted(path: str) -> None:
    assert is_public_dotted_path(path)


@pytest.mark.parametrize(
    "path",
    ["", ".", "files.", ".read", "files..read", "_private.read", "files._read", "a b.c", "1x.y"],
)
def test_anything_else_is_not_a_public_path(path: str) -> None:
    assert not is_public_dotted_path(path)


@pytest.mark.parametrize(("calls", "due"), [(0, True), (1, False), (2, True), (5, True)])
def test_a_layer_is_reported_unless_it_called_inner_exactly_once(calls: int, due: bool) -> None:
    assert activation_due(calls) is due


def _sync(*args: object) -> object:
    return None


async def _async(*args: object) -> object:
    return None


@pytest.mark.parametrize(
    ("current", "wrapper", "expected"),
    [(_sync, _sync, False), (_async, _sync, True), (_sync, _async, True), (_async, _async, True)],
)
def test_the_layer_is_async_when_either_side_is(
    current: Callable[..., object], wrapper: Callable[..., object], expected: bool
) -> None:
    assert layer_is_async(current, wrapper) is expected


def _read(path: str, *, mode: str = "r") -> str:
    return path + mode


_read.__doc__ = "Read a file."


class _Reader:
    def __init__(self) -> None:
        self.UnderstandError = ValueError

    def __call__(self, path: str) -> str:
        return path


def _transparent(inner: Callable[..., str], path: str, *, mode: str = "r") -> str:
    return inner(path, mode=mode)


def _documented(inner: Callable[..., str], path: str, *, label: str | None = None) -> str:
    return inner(path)


_documented.__doc__ = "Read a file and label it."


def test_the_chained_function_takes_its_identity_from_the_callable_it_replaces() -> None:
    class Replaced:
        def read(self, path: str) -> str:
            return path

    Replaced.read.__module__ = "plugin.files"
    face = chained_face(_documented, Replaced.read)
    assert (face.name, face.qualname, face.module) == (
        "read",
        f"{Replaced.__qualname__}.read",
        "plugin.files",
    )
    assert _documented.__module__ != "plugin.files"


def test_a_wrapper_that_wrote_a_docstring_owns_the_contract() -> None:
    assert chained_face(_documented, _read).doc == "Read a file and label it."


def test_a_transparent_wrapper_inherits_the_contract() -> None:
    assert chained_face(_transparent, _read).doc == "Read a file."


def test_function_attached_members_are_carried() -> None:
    assert chained_face(_transparent, _Reader()).inherited["UnderstandError"] is ValueError


def test_the_signature_is_the_wrappers_without_inner() -> None:
    signature = chained_face(_documented, _read).signature
    assert signature is not None
    assert str(signature) == "(path: 'str', *, label: 'str | None' = None) -> 'str'"


def test_a_wrapper_with_no_parameters_has_no_signature_to_show() -> None:
    def bare() -> None: ...

    assert chained_face(bare, _read).signature is None


def test_a_wrapper_that_cannot_be_introspected_has_no_signature_to_show() -> None:
    with pytest.raises(ValueError):
        inspect.signature(int)
    assert chained_face(int, _read).signature is None


def test_identity_falls_back_to_the_wrapper_and_then_to_a_default_name() -> None:
    nameless_current = functools.partial(_read, "x")
    named = chained_face(_documented, nameless_current)
    assert (named.name, named.qualname) == ("_documented", "_documented")
    nameless_wrapper = functools.partial(_documented)
    anonymous = chained_face(nameless_wrapper, nameless_current)
    assert (anonymous.name, anonymous.qualname) == ("wrapped", "wrapped")


def test_a_target_that_is_not_callable_is_refused_by_name_and_type() -> None:
    assert uncallable_message("shell.CONSTANT", 3) == (
        "meiva.shell.CONSTANT is int, not callable — wrap targets are functions."
    )
