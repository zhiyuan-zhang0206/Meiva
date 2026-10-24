# meiva:note
# The durable stop intent: which native birth was asked to stop, and the files that must never
# authorize another signal.

from __future__ import annotations

import json

import pytest

from cli.commands.data_plane.pooler.stop_intent import (
    NativeBirth,
    intent_names,
    native_birth,
    render_intent,
)

_LINUX = native_birth(1234, 1000.0, 9876)
_MAC = native_birth(1234, 1000.5, None)


# meiva:note
# On Linux the kernel start tick is the identity and the wall-clock birth is dropped, so a
# correction of the wall clock between two readers of one process does not make two births.
def test_a_linux_birth_is_its_start_tick_alone() -> None:
    assert NativeBirth(1234, None, 9876) == _LINUX
    assert native_birth(1234, 1001.0, 9876) == _LINUX


def test_a_birth_without_a_tick_is_its_start_time() -> None:
    assert NativeBirth(1234, 1000.5, None) == _MAC


@pytest.mark.parametrize("birth", [_LINUX, _MAC])
def test_a_rendered_intent_names_the_birth_it_was_rendered_for(birth: NativeBirth) -> None:
    assert intent_names(render_intent(birth), birth)


def test_an_intent_does_not_name_a_replacement_of_the_same_pid() -> None:
    assert not intent_names(render_intent(_LINUX), native_birth(1234, 1000.0, 9877))
    assert not intent_names(render_intent(_MAC), native_birth(1234, 1000.6, None))
    assert not intent_names(render_intent(_LINUX), native_birth(4321, 1000.0, 9876))


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        "null",
        json.dumps({"pid": 1234}),
        json.dumps({"pid": 1234, "birth": None, "starttime": None}),
        json.dumps({"pid": 1234, "birth": 1000.0, "starttime": 9876}),
        json.dumps({"pid": "1234", "birth": None, "starttime": 9876}),
        json.dumps({"pid": True, "birth": None, "starttime": 9876}),
        json.dumps({"pid": 1234, "birth": None, "starttime": 9876, "extra": 1}),
        json.dumps({"pid": 1234, "birth": "x", "starttime": None}),
    ],
)
def test_a_record_of_any_other_shape_never_authorizes_a_signal(text: str) -> None:
    with pytest.raises(RuntimeError, match="invalid PgBouncer stop intent; custody retained"):
        intent_names(text, _LINUX)
