from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from meiva.impersonation.harness.attach import ProcessProbe, refuse_unless_attachable
from meiva.impersonation.harness.registration import (
    FileRegistrationStore,
    RegistrationError,
    new_handle,
)


def ambient(
    *, lease: object | None = None, agent_id: int | None = None, owns_loop: bool = False
) -> Any:
    identity = SimpleNamespace(lease=lease, agent_id=agent_id, owns_loop=owns_loop)
    return SimpleNamespace(identity=identity)


def probe(*, host: bool = False, bound: Any = None, exec_turn: bool = False) -> ProcessProbe:
    return ProcessProbe(
        is_host=lambda: host,
        ambient=lambda: bound,
        in_exec_turn=lambda: exec_turn,
    )


def test_a_plain_process_may_attach() -> None:
    assert refuse_unless_attachable(probe()) is None


def test_a_native_runtime_may_not_attach() -> None:
    with pytest.raises(RuntimeError, match="native agent runtime"):
        refuse_unless_attachable(probe(host=True))


def test_a_process_that_owns_its_loop_may_not_attach() -> None:
    with pytest.raises(RuntimeError, match="native agent runtime"):
        refuse_unless_attachable(probe(bound=ambient(agent_id=41, owns_loop=True)))


def test_an_already_attached_process_may_not_attach_again() -> None:
    with pytest.raises(RuntimeError, match="already has an external attachment"):
        refuse_unless_attachable(probe(bound=ambient(lease=object())))


def test_an_exec_turn_may_not_attach() -> None:
    with pytest.raises(RuntimeError, match="exec turn"):
        refuse_unless_attachable(probe(exec_turn=True))


def test_a_bound_context_is_returned_for_reuse() -> None:
    bound = ambient()
    assert refuse_unless_attachable(probe(bound=bound)) is bound


def test_an_unknown_handle_finds_no_registration(tmp_path: Any) -> None:
    store = FileRegistrationStore(tmp_path)
    assert store.find_by_handle(new_handle()) is None


def test_a_missing_handle_says_which_variable_carries_it() -> None:
    from meiva.impersonation.harness.attach import attach

    with pytest.raises(RegistrationError, match="MEIVA_HARNESS_HANDLE"):
        attach("")
