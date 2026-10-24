# meiva:note
# The real borrowed-identity runtime the attachment owner tests share. Every part is the production
# one: a native agent row and checkpoint in the throwaway Postgres, a takeover lease the request,
# prepare and activate operations created, the SDK surface the real installer put into the
# process, a sample plugin the framework's own loader discovered, and a prior context built from
# the process clients. A test changes the world by the means the world offers (expire the lease
# row, journal a delta as another controller, write a different checkpoint) and observes the
# journal, the lease row, the receipts and the bound context.
# meiva:link meiva/impersonation/docs/attachment.meiva.okf.md

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver

import meiva
from agent.state import build_agent_state, plugin_state_schema
from agent.tests.impersonation.external.sample_plugin import (
    PLUGIN,
    forget_sample_plugin,
    write_sample_plugin,
)
from agent.tests.impersonation.lease_support import Cluster
from agent.tests.impersonation.lease_support import cluster as cluster
from base.agents import impersonation as leases
from base.agents.context.external_attachment.plugin_state_codec import encode_plugin_delta
from base.agents.impersonation.tests.impersonation_support import legacy_session
from base.agents.sdk.call_policy import SamplingPolicy, SamplingPolicyOwner
from base.clock import SYSTEM_TIMERS, Clock
from base.clock.configuration import clock_config_from_boot
from base.config import ConfigBoot
from base.config.service_read import ConfigAuthority
from base.config.tests.configuration import snapshot_process_config
from base.lm.catalog import ModelCatalog
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.packages.plugin_config_contracts import UnboundPluginConfigImages
from base.packages.plugins.extensions import ExtensionRegistry
from base.packages.plugins.loading import registry as plugin_registry
from meiva import impersonation
from meiva.boot import OwnedBoot, derived_clients, own_external_process
from meiva.sdk_surface import install, process_context
from meiva.sdk_surface import settings as _settings
from meiva.tests.fixtures.pin_agent import pin_agent

__all__ = [
    "Takeover",
    "assert_detached",
    "attached_mcp_runtime",
    "attached_runtime",
    "borrowed_agent_id",
    "cluster",
    "independent_boot",
    "legacy_runtime",
    "native_agent_with_checkpoint",
    "prior_context",
    "sample_surface",
    "state_cls",
]


def borrowed_agent_id() -> int | None:
    context = getattr(meiva, "context", None)
    lease = None if context is None or context.identity is None else context.identity.lease
    return None if lease is None else lease.agent_id


def assert_detached() -> None:
    assert not process_context.in_exec_turn()
    assert _settings.attached_agent_view() is None


def independent_boot() -> OwnedBoot:
    config = ConfigBoot()
    config.read_process_environment()
    return own_external_process(config)


_DROP_CHECKPOINT = (
    "DELETE FROM checkpoint_writes WHERE thread_id=%s",
    "DELETE FROM checkpoint_blobs WHERE thread_id=%s",
    "DELETE FROM checkpoints WHERE thread_id=%s",
)


# meiva:note
# Plugin discovery admits every plugin whose face module this process has already imported, so a
# test that ran earlier in the same worker would add its plugins to the registry. These tests
# install the one plugin they wrote.
def sample_registry() -> ExtensionRegistry:
    discovered = plugin_registry.build_registry(validate_state=plugin_state_schema)
    return ExtensionRegistry(tuple(item for item in discovered.plugins if item[0] == PLUGIN))


def native_state_class() -> type[Any]:
    return build_agent_state(sample_registry())


# meiva:note
# One active takeover of a native agent whose checkpoint is in the database.
@dataclass(frozen=True)
class Takeover:
    cluster: Cluster
    db_url: str
    owner: RuntimeIncarnation
    lease_id: str

    @property
    def agent_id(self) -> int:
        return self.owner.agent_id

    @property
    def machine(self) -> str:
        return self.cluster.local_machine

    def lease(self) -> dict[str, Any]:
        return self.cluster.get(self.lease_id)

    def journal(self) -> list[dict[str, str]]:
        return self.lease()["plugin_delta"]

    def write_checkpoint(self, **channels: object) -> None:
        self.write_channels({"sample__seen": {"native"}, **channels})

    def write_channels(self, values: dict[str, object]) -> None:
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = values
        versions: dict[str, str | int | float] = dict.fromkeys(values, "1")
        checkpoint["channel_versions"] = versions
        with PostgresSaver.from_conn_string(self.db_url) as saver:
            saver.setup()
            saver.put(
                {"configurable": {"thread_id": str(self.agent_id), "checkpoint_ns": ""}},
                checkpoint,
                {"source": "input", "step": 1, "parents": {}},
                versions,
            )

    def drop_checkpoint(self) -> None:
        for statement in _DROP_CHECKPOINT:
            self.cluster.db_conn.execute(statement, (str(self.agent_id),))
        self.cluster.db_conn.commit()

    def expire(self) -> None:
        self.cluster.db_conn.execute(
            "UPDATE agent_impersonations SET expires_at=clock_timestamp()-interval '1 second' "
            "WHERE id=%s",
            (self.lease_id,),
        )
        self.cluster.db_conn.commit()

    def declare_no_capabilities(self) -> None:
        self.cluster.db_conn.execute(
            "UPDATE agent_impersonations SET capabilities='[]'::jsonb, "
            "capability_declared_by='caller-default' WHERE id=%s",
            (self.lease_id,),
        )
        self.cluster.db_conn.commit()

    def replacement_lease(self) -> str:
        return self.cluster.lease(self.owner, phase="active")["id"]

    def journal_as_another_controller(self, delta: dict[str, Any]) -> None:
        leases.merge_plugin_delta(
            self.cluster.database,
            self.lease_id,
            encode_plugin_delta(delta, native_state_class()),
            expected_version=self.lease()["delta_version"],
            local_machine=self.machine,
        )

    def receipts(self) -> list[str]:
        rows = self.cluster.db_conn.execute(
            "SELECT state FROM agent_impersonation_event_participants "
            "WHERE lease_id=%s ORDER BY source_key",
            (self.lease_id,),
        ).fetchall()
        self.cluster.db_conn.commit()
        return [str(state) for (state,) in rows]


@pytest.fixture
def sample_surface(
    unit_home: Path,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    request: pytest.FixtureRequest,
) -> Iterator[None]:
    write_sample_plugin(unit_home / "plugins")
    request.addfinalizer(forget_sample_plugin)
    sampling = SamplingPolicyOwner(reader=SamplingPolicy)
    install.install(
        sample_registry(),
        config_images=UnboundPluginConfigImages(),
        catalog=model_catalog,
        authority=config_authority,
        sampling=sampling,
    )
    if getattr(request, "param", "faces") == "faces":
        install.mark_faces_loaded()
    try:
        yield
    finally:
        install.uninstall()
        sampling.close()
        process_context.unbind_exec_turn()


@pytest.fixture
def state_cls(sample_surface: None) -> type[Any]:
    return native_state_class()


def native_agent_with_checkpoint(
    cluster: Cluster, db_url: str, *, legacy_lease: bool = False
) -> Takeover:
    owner = cluster.native_agent()
    cluster.db_conn.execute(
        "UPDATE agents_meta SET config_overlay=%s::jsonb WHERE id=%s",
        ('{"llm_model": "external-test"}', owner.agent_id),
    )
    cluster.db_conn.commit()
    if legacy_lease:
        lease = legacy_session(cluster.db_conn, owner, local_machine=cluster.local_machine)
    else:
        lease = cluster.lease(owner, phase="active")
    takeover = Takeover(cluster, db_url, owner, lease["id"])
    takeover.write_checkpoint()
    return takeover


@pytest.fixture
def prior_context(cluster: Cluster, request: pytest.FixtureRequest) -> Iterator[None]:
    config = snapshot_process_config()
    clients = derived_clients(config, cluster.machine)
    pin_agent(
        None,
        owns_loop=True,
        clients=clients,
        machine=cluster.machine,
        clock_factory=lambda: Clock(clock_config_from_boot(config), timers=SYSTEM_TIMERS),
    )
    process_context.unbind_exec_turn()
    request.addfinalizer(process_context.unbind_exec_turn)
    try:
        yield
    finally:
        clients.close()


@pytest.fixture
def attached_runtime(
    sample_surface: None, prior_context: None, cluster: Cluster, db_url: str
) -> Takeover:
    return native_agent_with_checkpoint(cluster, db_url)


@pytest.fixture
def legacy_runtime(
    sample_surface: None, prior_context: None, cluster: Cluster, db_url: str
) -> Takeover:
    return native_agent_with_checkpoint(cluster, db_url, legacy_lease=True)


@pytest.fixture
def attached_mcp_runtime(attached_runtime: Takeover) -> Iterator[tuple[Takeover, Any]]:
    attachment = impersonation.attach(attached_runtime.lease_id)
    try:
        yield attached_runtime, attachment
    finally:
        attachment.close()
