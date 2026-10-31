from __future__ import annotations

import base64
import os
import secrets
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from base.agents.context import MeivaContext
from base.agents.context.identity import AgentIdentity, ExternalLease
from meiva.sdk_surface import process_context
from meiva.self.harness.admission import ProcessLineage, descends, require_live_registration
from meiva.self.harness.attachment import (
    attachable_context,
    process_lineage,
    resolve_registration,
)
from meiva.self.harness.registration import (
    HarnessRegistration,
    RegistrationError,
    handle_digest,
)
from meiva.self.harness.store import FileRegistrationStore

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
LINEAGE = ProcessLineage(pid=900, ancestors=frozenset({800, 4242}))
_SLEEPER = Path(__file__).with_name("sleeper_child.py")


@pytest.fixture
def unbound_process() -> Iterator[None]:
    prior = process_context.unbind_context()
    try:
        yield
    finally:
        process_context.unbind_context()
        if prior is not None:
            process_context.bind_context(prior)


@pytest.mark.usefixtures("unbound_process")
def test_an_unbound_plain_process_has_no_context_to_reuse() -> None:
    assert attachable_context() is None


@pytest.mark.usefixtures("unbound_process")
def test_a_bound_context_is_returned_for_reuse() -> None:
    bound = MeivaContext()
    process_context.bind_context(bound)
    assert attachable_context() is bound


@pytest.mark.usefixtures("unbound_process")
def test_a_bound_native_loop_is_refused() -> None:
    process_context.bind_context(MeivaContext(identity=AgentIdentity(agent_id=41, owns_loop=True)))
    with pytest.raises(RuntimeError, match="native agent runtime"):
        attachable_context()


@pytest.mark.usefixtures("unbound_process")
def test_a_bound_borrowed_identity_is_refused() -> None:
    lease = ExternalLease(agent_id=41, validate=lambda: 41, config=lambda: None)
    process_context.bind_context(
        MeivaContext(identity=AgentIdentity(agent_id=None, owns_loop=True, lease=lease))
    )
    with pytest.raises(RuntimeError, match="already has an external attachment"):
        attachable_context()


def row(**overrides: Any) -> HarnessRegistration:
    values: dict[str, Any] = {
        "agent_id": 41,
        "harness": "deepseek",
        "external_session_id": "session-1",
        "machine": "ubuntu",
        "controller_pid": 4242,
        "handle_sha256": handle_digest("hx_test"),
        "created_at": "2026-10-19T00:00:00+00:00",
    }
    values.update(overrides)
    return HarnessRegistration(**values)


def admit(registration: HarnessRegistration, **overrides: Any) -> HarnessRegistration:
    arguments: dict[str, Any] = {"machine": "ubuntu", "lineage": LINEAGE, "now": NOW}
    arguments.update(overrides)
    return require_live_registration(registration, **arguments)


def test_a_live_registration_passes() -> None:
    registration = row()
    assert admit(registration) is registration


def test_a_detached_registration_is_refused() -> None:
    with pytest.raises(RegistrationError, match="is closed"):
        admit(row(status="closed"))


def test_an_expired_registration_is_refused() -> None:
    with pytest.raises(RegistrationError, match="expired at"):
        admit(row(expires_at="2026-10-18T00:00:00+00:00"))


def test_expiry_is_judged_at_the_supplied_instant() -> None:
    registration = row(expires_at="2026-10-19T12:00:00+00:00")
    before = datetime(2026, 10, 19, 11, 0, tzinfo=UTC)
    after = datetime(2026, 10, 19, 13, 0, tzinfo=UTC)
    assert admit(registration, now=before)
    with pytest.raises(RegistrationError, match="expired at"):
        admit(registration, now=after)


def test_another_machines_registration_is_refused() -> None:
    with pytest.raises(RegistrationError, match="belongs to machine 'macmini'"):
        admit(row(machine="macmini"))


def test_a_process_outside_the_controller_tree_is_refused() -> None:
    with pytest.raises(RegistrationError, match="does not descend"):
        admit(row(controller_pid=1234))


def test_the_controller_itself_is_admitted() -> None:
    assert admit(row(controller_pid=LINEAGE.pid))


@pytest.mark.parametrize(
    ("controller_pid", "expected"),
    [(900, True), (800, True), (4242, True), (1234, False), (0, False), (-1, False)],
)
def test_descent_is_the_process_itself_or_one_of_its_ancestors(
    controller_pid: int, expected: bool
) -> None:
    assert descends(controller_pid, LINEAGE) is expected


def test_the_real_lineage_of_this_process_contains_its_parent() -> None:
    lineage = process_lineage(os.getpid())
    assert lineage.pid == os.getpid()
    assert os.getppid() in lineage.ancestors
    assert descends(os.getpid(), lineage)
    assert descends(os.getppid(), lineage)
    assert not descends(2**22 + 7, lineage)


def test_an_unknown_process_has_no_ancestors() -> None:
    assert process_lineage(2**22 + 7).ancestors == frozenset()


# meiva:note
# The child only sleeps (`sleeper_child.py`); `sys.executable` is the test's own interpreter and the argv is
# fixed, so the subprocess call carries no shell and no caller input.
def test_a_child_process_descends_from_the_process_that_started_it() -> None:
    child = subprocess.Popen([sys.executable, str(_SLEEPER)])  # noqa: S603
    try:
        lineage = process_lineage(child.pid)
        assert descends(os.getpid(), lineage)
        assert not descends(child.pid + 1, lineage)
    finally:
        child.kill()
        child.wait()


def test_a_handle_issued_for_another_agent_is_refused(tmp_path: Path) -> None:
    store = FileRegistrationStore(tmp_path)
    store.save(row(handle_sha256=handle_digest("hx_mine")))
    with pytest.raises(RegistrationError, match="belongs to agent 41, not 42"):
        resolve_registration("hx_mine", agent_id=42, store=store)


def test_a_handle_issued_for_another_session_is_refused(tmp_path: Path) -> None:
    store = FileRegistrationStore(tmp_path)
    store.save(row(handle_sha256=handle_digest("hx_mine")))
    with pytest.raises(RegistrationError, match="another external session"):
        resolve_registration("hx_mine", session_id="session-2", store=store)


def test_a_matching_handle_agent_and_session_resolve_the_row(tmp_path: Path) -> None:
    store = FileRegistrationStore(tmp_path)
    saved = row(handle_sha256=handle_digest("hx_mine"))
    store.save(saved)
    resolved = resolve_registration("hx_mine", agent_id=41, session_id="session-1", store=store)
    assert resolved == saved


def test_a_handle_alone_resolves_without_naming_the_agent(tmp_path: Path) -> None:
    store = FileRegistrationStore(tmp_path)
    saved = row(handle_sha256=handle_digest("hx_mine"))
    store.save(saved)
    assert resolve_registration("hx_mine", store=store) == saved


def test_an_unknown_handle_finds_no_registration(tmp_path: Path) -> None:
    with pytest.raises(RegistrationError, match="matches this handle"):
        resolve_registration(_handle(), store=FileRegistrationStore(tmp_path))


# meiva:note
# A handle shaped like the one the bridge mints: 32 random bytes, URL-safe base64. The store keeps
# only its digest, so the value itself is the test's to choose.
def _handle() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
