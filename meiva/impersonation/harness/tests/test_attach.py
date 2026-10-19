from __future__ import annotations

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from meiva.impersonation.harness.attach import (
    ProcessProbe,
    refuse_unless_attachable,
    require_live_registration,
    resolve_registration,
)
from meiva.impersonation.harness.registration import (
    FileRegistrationStore,
    HarnessRegistration,
    RegistrationError,
    handle_digest,
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


def row(**overrides: Any) -> HarnessRegistration:
    values: dict[str, Any] = {
        "agent_id": 41,
        "harness": "deepseek",
        "external_session_id": "session-1",
        "machine": "ubuntu",
        "controller_pid": os.getpid(),
        "handle_sha256": handle_digest("hx_test"),
        "created_at": "2026-10-19T00:00:00+00:00",
    }
    values.update(overrides)
    return HarnessRegistration(**values)


def test_a_live_registration_passes() -> None:
    registration = row()
    assert (
        require_live_registration(registration, machine="ubuntu", descends=lambda _pid: True)
        is registration
    )


def test_a_detached_registration_is_refused() -> None:
    with pytest.raises(RegistrationError, match="is closed"):
        require_live_registration(
            row(status="closed"), machine="ubuntu", descends=lambda _pid: True
        )


def test_an_expired_registration_is_refused() -> None:
    expired = row(expires_at="2026-10-18T00:00:00+00:00")
    with pytest.raises(RegistrationError, match="expired at"):
        require_live_registration(expired, machine="ubuntu", descends=lambda _pid: True)


def test_expiry_is_judged_at_the_supplied_instant() -> None:
    registration = row(expires_at="2026-10-19T12:00:00+00:00")
    before = datetime(2026, 10, 19, 11, 0, tzinfo=UTC)
    after = datetime(2026, 10, 19, 13, 0, tzinfo=UTC)
    assert require_live_registration(
        registration, machine="ubuntu", descends=lambda _pid: True, now=before
    )
    with pytest.raises(RegistrationError, match="expired at"):
        require_live_registration(
            registration, machine="ubuntu", descends=lambda _pid: True, now=after
        )


def test_another_machines_registration_is_refused() -> None:
    with pytest.raises(RegistrationError, match="belongs to machine 'macmini'"):
        require_live_registration(
            row(machine="macmini"), machine="ubuntu", descends=lambda _pid: True
        )


def test_a_process_outside_the_controller_tree_is_refused() -> None:
    registration = row(controller_pid=4242)
    with pytest.raises(RegistrationError, match="does not descend"):
        require_live_registration(registration, machine="ubuntu", descends=lambda _pid: False)


def test_the_ancestry_check_receives_the_recorded_controller_pid() -> None:
    seen: list[int] = []

    def descends(pid: int) -> bool:
        seen.append(pid)
        return True

    require_live_registration(row(controller_pid=4242), machine="ubuntu", descends=descends)
    assert seen == [4242]


def test_a_handle_issued_for_another_agent_is_refused(tmp_path: Any) -> None:
    store = FileRegistrationStore(tmp_path)
    store.save(row(handle_sha256=handle_digest("hx_mine")))
    with pytest.raises(RegistrationError, match="belongs to agent 41, not 42"):
        resolve_registration("hx_mine", agent_id=42, store=store)


def test_a_handle_issued_for_another_session_is_refused(tmp_path: Any) -> None:
    store = FileRegistrationStore(tmp_path)
    store.save(row(handle_sha256=handle_digest("hx_mine")))
    with pytest.raises(RegistrationError, match="another external session"):
        resolve_registration("hx_mine", session_id="session-2", store=store)


def test_a_matching_handle_agent_and_session_resolve_the_row(tmp_path: Any) -> None:
    store = FileRegistrationStore(tmp_path)
    saved = row(handle_sha256=handle_digest("hx_mine"))
    store.save(saved)
    resolved = resolve_registration("hx_mine", agent_id=41, session_id="session-1", store=store)
    assert resolved == saved


def test_a_handle_alone_resolves_without_naming_the_agent(tmp_path: Any) -> None:
    store = FileRegistrationStore(tmp_path)
    saved = row(handle_sha256=handle_digest("hx_mine"))
    store.save(saved)
    assert resolve_registration("hx_mine", store=store) == saved
