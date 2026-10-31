# meiva:note
# The borrowed identity of a takeover attachment: what it grants while the lease is valid, what
# stops it the moment the lease expires or another controller changes the plugin state, and that
# a failed or repeated entry, close or construction leaves the process exactly as it found it.
# The lease, the checkpoint and the receipts are the real rows in the throwaway Postgres.
# meiva:link meiva/impersonation/docs/attachment.meiva.okf.md

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

import psycopg
import pytest

import meiva
from agent import state as state_module
from agent.tests.impersonation.external.runtime import Takeover, assert_detached, borrowed_agent_id
from agent.tests.impersonation.external.sample_plugin import ExamplePlugin
from base.agents.context.external_attachment.plugin_state_codec import load_snapshot
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config.tests.configuration import snapshot_process_config
from base.paths import meiva_home
from meiva import impersonation
from meiva.sdk_surface import agent_identity, process_context
from meiva.sdk_surface.installation import installed
from meiva.sdk_surface.settings import agent_setting
from meiva.tests.fixtures.pin_agent import pin_agent

HANDSHAKE_BOUND_S = 30.0


def test_attach_borrows_the_identity_and_gives_it_back_on_detach(
    attached_runtime: Takeover,
) -> None:
    agent_id = attached_runtime.agent_id
    with impersonation.attach(attached_runtime.lease_id):
        assert borrowed_agent_id() == agent_id
        assert agent_id == meiva.self.AGENT_ID
        assert agent_identity.require_agent_id() == agent_id
        assert agent_identity.require_actor() == f"agent:{agent_id}"
        assert agent_setting("llm_model") == "external-test"
    assert borrowed_agent_id() is None
    with pytest.raises(RuntimeError, match="no established actor"):
        agent_identity.require_actor()
    assert_detached()


def test_expiry_blocks_identity_and_plugin_state_before_new_effects(
    attached_runtime: Takeover,
) -> None:
    attachment = impersonation.attach(attached_runtime.lease_id)
    handle = state_module.PluginStateHandle(ExamplePlugin, "sample")
    attached_runtime.expire()
    for read in (
        lambda: meiva.self.AGENT_ID,
        agent_identity.require_actor,
        handle.read,
        lambda: handle.update({"seen": {"too-late"}}),
    ):
        with pytest.raises(RuntimeError, match="not active"):
            read()
    with pytest.raises(RuntimeError, match="not active"):
        attachment.close()
    assert borrowed_agent_id() is None
    assert attached_runtime.journal() == []


def test_stale_attachment_refuses_sdk_identity_and_removes_identity_on_close(
    attached_runtime: Takeover,
) -> None:
    attachment = impersonation.attach(attached_runtime.lease_id)
    attached_runtime.journal_as_another_controller({"sample__seen": {"other"}})
    with pytest.raises(RuntimeError, match="another attachment"):
        agent_identity.require_actor()
    with pytest.raises(RuntimeError, match="another attachment"):
        attachment.close()
    assert borrowed_agent_id() is None


def test_attach_refuses_inside_an_exec_turn(
    attached_runtime: Takeover, state_cls: type[Any]
) -> None:
    process_context.set_state(state_cls(sample__seen={"prior"}))
    with pytest.raises(RuntimeError, match="exec turn cannot attach"):
        impersonation.attach(attached_runtime.lease_id)
    assert borrowed_agent_id() is None


def test_attach_on_another_machine_binds_no_identity(attached_runtime: Takeover) -> None:
    elsewhere = snapshot_process_config()
    elsewhere.set_field("machine_name", "another-runner")
    pin_agent(None, owns_loop=True, machine=ConfiguredMachineIdentity(elsewhere))
    with pytest.raises(RuntimeError, match="another-runner"):
        impersonation.attach(attached_runtime.lease_id)
    assert borrowed_agent_id() is None


@pytest.mark.parametrize("invalidated", ["expiry", "state_version"])
def test_failed_context_entry_detaches_and_allows_next_attachment(
    attached_runtime: Takeover, invalidated: str
) -> None:
    attachment = impersonation.attach(attached_runtime.lease_id)
    state_module.PluginStateHandle(ExamplePlugin, "sample").update({"seen": {"unflushed"}})
    if invalidated == "expiry":
        attached_runtime.expire()
        reason = "not active"
    else:
        attached_runtime.journal_as_another_controller({"sample__seen": {"other"}})
        reason = "another attachment"
    with pytest.raises(RuntimeError, match=reason), attachment:
        pytest.fail("an invalid attachment entered its context")
    assert borrowed_agent_id() is None
    assert_detached()
    assert all("unflushed" not in str(entry) for entry in attached_runtime.journal())
    attachment.close()
    if invalidated == "expiry":
        with pytest.raises(RuntimeError, match="not active"):
            impersonation.attach(attached_runtime.lease_id)
    else:
        with impersonation.attach(attached_runtime.lease_id):
            assert borrowed_agent_id() == attached_runtime.agent_id
            assert attached_runtime.agent_id == meiva.self.AGENT_ID
    assert_detached()


def lock_lease_reads(conn: psycopg.Connection[Any]) -> None:
    conn.execute("LOCK TABLE agent_impersonations IN ACCESS EXCLUSIVE MODE")


def wait_for_blocked_lease_read(conn: psycopg.Connection[Any]) -> None:
    deadline = time.monotonic() + HANDSHAKE_BOUND_S
    while time.monotonic() < deadline:
        waiting = conn.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type='Lock' "
            "AND query ILIKE '%agent_impersonations%' AND pid <> pg_backend_pid()"
        ).fetchone()
        if waiting is not None and waiting[0] > 0:
            return
        time.sleep(0.02)
    pytest.fail("the first constructor did not reach its lease lookup")


def test_concurrent_constructor_fails_before_lease_lookup(attached_runtime: Takeover) -> None:
    holder = psycopg.connect(attached_runtime.db_url)
    observer = psycopg.connect(attached_runtime.db_url, autocommit=True)
    lease_id, agent_id = attached_runtime.lease_id, attached_runtime.agent_id
    try:
        lock_lease_reads(holder)
        with ThreadPoolExecutor(max_workers=1) as executor:

            def attach_in_worker() -> None:
                with impersonation.attach(lease_id):
                    assert borrowed_agent_id() == agent_id

            first = executor.submit(attach_in_worker)
            try:
                wait_for_blocked_lease_read(observer)
                with (
                    pytest.raises(RuntimeError, match="already has an external attachment"),
                    impersonation.attach(lease_id),
                ):
                    pass
            finally:
                holder.rollback()
                first.result(timeout=HANDSHAKE_BOUND_S)
    finally:
        holder.close()
        observer.close()
    assert borrowed_agent_id() is None
    with impersonation.attach(lease_id):
        assert borrowed_agent_id() == agent_id


@pytest.mark.parametrize("failure_at", ["lease", "snapshot"])
def test_constructor_failure_detaches_and_allows_next_attachment(
    attached_runtime: Takeover, failure_at: str
) -> None:
    if failure_at == "lease":
        with pytest.raises(RuntimeError, match="does not exist"):
            impersonation.attach("00000000-0000-0000-0000-000000000000")
    else:
        attached_runtime.drop_checkpoint()
        with pytest.raises(RuntimeError, match="no checkpoint"):
            impersonation.attach(attached_runtime.lease_id)
        attached_runtime.write_checkpoint()
    assert borrowed_agent_id() is None
    assert_detached()
    assert attached_runtime.journal() == []
    with impersonation.attach(attached_runtime.lease_id):
        assert borrowed_agent_id() == attached_runtime.agent_id


def test_repeated_close_cannot_release_another_attachment(attached_runtime: Takeover) -> None:
    first = impersonation.attach(attached_runtime.lease_id)
    first.close()
    with impersonation.attach(attached_runtime.lease_id):
        first.close()
        assert borrowed_agent_id() == attached_runtime.agent_id
        with pytest.raises(RuntimeError, match="already has an external attachment"):
            impersonation.attach(attached_runtime.lease_id)


def test_attachment_reuses_clients_and_restores_original_context(
    attached_runtime: Takeover,
) -> None:
    original = getattr(meiva, "context", None)
    assert original is not None
    with impersonation.attach(attached_runtime.lease_id):
        assert meiva.context.clients is original.clients
        assert meiva.context.identity is not None
        assert meiva.context.identity.lease is not None
        assert meiva.context.identity.lease.agent_id == attached_runtime.agent_id
        assert meiva.context.sql is meiva.DB
        assert meiva.context.redis is meiva.REDIS
    assert getattr(meiva, "context", None) is original


def test_attach_replays_the_journal_a_previous_controller_left(attached_runtime: Takeover) -> None:
    handle = state_module.PluginStateHandle(ExamplePlugin, "sample")
    attached_runtime.journal_as_another_controller({"sample__seen": {"earlier"}})
    with impersonation.attach(attached_runtime.lease_id):
        assert handle.read().seen == {"native", "earlier"}


def test_a_log_native_lease_opens_one_receipt_and_seals_it_on_close(
    attached_runtime: Takeover,
) -> None:
    attachment = impersonation.attach(attached_runtime.lease_id)
    assert attached_runtime.receipts() == ["open"]
    attachment.close()
    assert attached_runtime.receipts() == ["sealed"]


def test_a_lease_that_predates_the_event_log_opens_no_receipt(legacy_runtime: Takeover) -> None:
    with impersonation.attach(legacy_runtime.lease_id):
        assert legacy_runtime.receipts() == []
    assert legacy_runtime.receipts() == []


@pytest.mark.parametrize("sample_surface", ["surface_only"], indirect=True)
def test_attach_loads_the_native_faces_when_only_the_surface_is_installed(
    attached_runtime: Takeover,
) -> None:
    installation = installed()
    assert installation is not None and not installation.faces
    with impersonation.attach(attached_runtime.lease_id):
        current = installed()
        assert current is not None and current.faces


def test_the_native_snapshot_is_the_checkpoint_the_attachment_never_writes(
    attached_runtime: Takeover,
) -> None:
    handle = state_module.PluginStateHandle(ExamplePlugin, "sample")
    with impersonation.attach(attached_runtime.lease_id) as attachment:
        handle.update({"seen": {"external"}})
        assert cast(Any, attachment.native_snapshot()).sample__seen == {"native"}
    stored, _, _ = load_snapshot(
        attached_runtime.agent_id, database=attached_runtime.cluster.database, home=meiva_home()
    )
    assert stored.sample__seen == {"native"}


def test_external_controls_stay_out_of_the_native_prompt() -> None:
    from agent.graph.prompt.system_prompt import build_system_prompt
    from base.config.tests.configuration import defaults
    from base.host.env.agent_slices import AgentSlices
    from base.packages.plugin_providers import build_model_catalog
    from base.packages.plugins.extensions import ExtensionRegistry

    prompt = build_system_prompt(
        ExtensionRegistry(()),
        AgentSlices.resolve(default_reader=defaults()),
        agent_id=None,
        catalog=build_model_catalog(home=meiva_home()),
        home=meiva_home(),
    )
    assert "impersonation" not in meiva.__all_for_meiva__
    assert "meiva.impersonation.attach" not in prompt
    assert "## meiva.impersonation" not in prompt
