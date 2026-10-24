import stat
from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver

import meiva
from base.agents import impersonation as leases
from base.agents.impersonation.tests.impersonation_support import recorded_tree
from base.agents.sdk.call_policy import SamplingPolicy, SamplingPolicyOwner
from base.api_contracts.caller_identity import CallerIdentity
from base.clock import SYSTEM_TIMERS, Clock
from base.clock.configuration import clock_config_from_boot
from base.cluster.machine import MachineIdentity
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.config.tests.configuration import snapshot_process_config
from base.db import Database, create_agent
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.packages.plugins.extensions import ExtensionRegistry
from base.paths import workspace_dir_readonly
from meiva import impersonation
from meiva.boot import process_clients
from meiva.sdk_surface import install, process_context
from meiva.sdk_surface.installation import Installation
from meiva.tests.fixtures.pin_agent import pin_agent, pin_no_identity


def _extensions() -> ExtensionRegistry:
    from meiva_builtins.plugins.meiva_code import plugin as code_plugin
    from meiva_builtins.plugins.meiva_memory import plugin as memory_plugin

    return ExtensionRegistry(
        (
            ("meiva_code", code_plugin.contribute()),
            ("meiva_memory", memory_plugin.contribute()),
        )
    )


@pytest.fixture
def native_checkpoint(
    db_conn: psycopg.Connection,
    request: pytest.FixtureRequest,
    model_installation: Installation,
    machine_identity: MachineIdentity,
) -> Iterator[RuntimeIncarnation]:
    pin_no_identity()
    process_context.unbind_exec_turn()
    request.addfinalizer(process_context.unbind_exec_turn)

    install.install(
        _extensions(),
        config_images=model_installation.config_images,
        catalog=model_installation.catalog,
        authority=model_installation.authority,
        sampling=SamplingPolicyOwner(reader=SamplingPolicy),
    )
    install.mark_faces_loaded()
    request.addfinalizer(install.uninstall)

    agent_id = create_agent(db_conn)
    owner = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_generation,runtime_owner) "
        "VALUES(%s,'idling',%s,%s,%s)",
        (agent_id, machine_identity.name(), owner.generation, owner.owner),
    )
    db_conn.commit()
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"messages": [HumanMessage(content="Native task")]}
    versions: dict[str, str | int | float] = {"messages": "1"}
    checkpoint["channel_versions"] = versions
    with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
        saver.setup()
        saver.put(
            {"configurable": {"thread_id": str(agent_id), "checkpoint_ns": ""}},
            checkpoint,
            {"source": "input", "step": 1, "parents": {}},
            versions,
        )
    config = snapshot_process_config()
    with closing(process_clients(config=config, machine=machine_identity)) as clients:
        pin_agent(
            None,
            owns_loop=False,
            clients=clients,
            machine=machine_identity,
            clock_factory=lambda: Clock(clock_config_from_boot(config), timers=SYSTEM_TIMERS),
        )
        yield owner


@pytest.mark.parametrize("stale_process_identity", [False, True])
def test_external_personal_path_uses_borrowed_identity(
    native_checkpoint: RuntimeIncarnation,
    unit_home: Path,
    stale_process_identity: bool,
    database: Database,
    event_bus: EventBus,
    config_authority: ConfigAuthority,
    *,
    machine_identity: MachineIdentity,
) -> None:
    owner = native_checkpoint
    stale_id = owner.agent_id + 1000
    pin_agent(stale_id if stale_process_identity else None, owns_loop=False)
    lease = leases.request(
        database,
        event_bus,
        owner.agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex"),
        process_metadata=recorded_tree(),
        relay_provider="codex",
        relay_thread_id=str(uuid4()),
        authority=config_authority,
        local_machine=machine_identity.name(),
        name="Test name",
        executor_name="Test executor_name",
    )
    leases.prepare(
        database,
        event_bus,
        lease["id"],
        owner.agent_id,
        owner,
        "Handoff brief",
        local_machine=machine_identity.name(),
    )
    leases.activate(database, event_bus, lease["id"], owner)

    with impersonation.attach(lease["id"]):
        personal = meiva.memory.PERSONAL_PATH

    assert personal == workspace_dir_readonly(owner.agent_id) / "memory"
    assert not workspace_dir_readonly(stale_id).exists()


# meiva:note
# Every entry under `root` with what a filesystem effect leaves behind: its path, kind, size and
# modification time. A directory's modification time moves when an entry is added or removed in
# it, and a file's moves when it is rewritten, so recreating a directory or overwriting a file
# that was already there changes the snapshot too.
def _tree(root: Path) -> list[tuple[str, str, int, int]]:
    entries: list[tuple[str, str, int, int]] = []
    for path in sorted(root.rglob("*")):
        status = path.lstat()
        kind = "link" if stat.S_ISLNK(status.st_mode) else "dir" if path.is_dir() else "file"
        entries.append((str(path.relative_to(root)), kind, status.st_size, status.st_mtime_ns))
    return entries


@pytest.mark.parametrize("operation", ["personal_path", "note"])
def test_external_memory_rechecks_lease_before_filesystem_effects(
    native_checkpoint: RuntimeIncarnation,
    db_conn: psycopg.Connection,
    unit_home: Path,
    operation: str,
    database: Database,
    event_bus: EventBus,
    config_authority: ConfigAuthority,
    *,
    machine_identity: MachineIdentity,
) -> None:
    from meiva_builtins.plugins.meiva_memory import notes, sdk

    owner = native_checkpoint
    stale_process_id = owner.agent_id + 1000
    pin_agent(stale_process_id, owns_loop=False)
    lease = leases.request(
        database,
        event_bus,
        owner.agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex"),
        process_metadata=recorded_tree(),
        relay_provider="codex",
        relay_thread_id=str(uuid4()),
        authority=config_authority,
        local_machine=machine_identity.name(),
        name="Test name",
        executor_name="Test executor_name",
    )
    leases.prepare(
        database,
        event_bus,
        lease["id"],
        owner.agent_id,
        owner,
        "Handoff brief",
        local_machine=machine_identity.name(),
    )
    leases.activate(database, event_bus, lease["id"], owner)
    attachment = impersonation.attach(lease["id"])
    try:
        db_conn.execute(
            "UPDATE agent_impersonations SET expires_at=clock_timestamp()-interval '1 second' "
            "WHERE id=%s",
            (lease["id"],),
        )
        db_conn.commit()
        workspaces = unit_home / "workspaces"
        before = _tree(workspaces)
        with pytest.raises(leases.ImpersonationError, match="expired"):
            if operation == "personal_path":
                _ = sdk.PERSONAL_PATH
            else:
                context = replace(
                    meiva.context,
                    agent=AgentSlices.resolve(
                        {"memory_per_agent_inject_enabled": True},
                        default_reader=lambda _domain, field: config_authority.service_field_value(
                            field
                        ),
                    ),
                )
                notes.per_agent_memory_note(context)
        assert _tree(workspaces) == before
        assert not (workspaces / str(stale_process_id)).exists()
    finally:
        with pytest.raises(leases.ImpersonationError, match="expired"):
            attachment.close()
