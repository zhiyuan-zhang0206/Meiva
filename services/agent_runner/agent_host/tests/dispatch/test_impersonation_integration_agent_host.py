import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent import impersonation_process
from agent.ownership.hosted import admit_hosted_runtime
from base.agents import impersonation as leases
from base.agents.impersonation.tests.impersonation_support import recorded_tree
from base.api_contracts.caller_identity import CallerIdentity
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.packages.composer_commands import CommandCatalog
from services.agent_runner.agent_host.tests.host_policy import configured_machine, configured_policy


async def test_replacement_host_admission_ends_the_takeover_without_respawning_its_relay(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    from services.agent_runner.agent_host.host import AgentHost

    agent_id = create_agent(db_conn)
    machine = machine_identity.name()
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine) VALUES(%s,'idling',%s)", (agent_id, machine)
    )
    db_conn.commit()
    owner = await admit_hosted_runtime(aops_pool, agent_id, machine, uuid4(), db=database)
    assert owner is not None
    lease = leases.request(
        database,
        event_bus,
        agent_id,
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
        agent_id,
        owner,
        "Handoff brief",
        local_machine=machine_identity.name(),
    )
    leases.activate(database, event_bus, lease["id"], owner)
    spawn = MagicMock(return_value=MagicMock(poll=MagicMock(return_value=None)))
    monkeypatch.setattr(impersonation_process, "spawn_codex_relay", spawn)
    graph = _HandoffGraph()
    host = AgentHost(
        policy=configured_policy(),
        pool=aops_pool,
        checkpointer=MagicMock(),
        graph=cast(Any, graph),
        machine=configured_machine(name=machine),
        bus=EventBus.from_settings(),
        db=Database.from_settings(gate=database_gate),
        catalog=model_catalog,
        commands=CommandCatalog(skills=list),
    )
    await host.run_turn(agent_id)
    assert db_conn.execute(
        "SELECT runtime_owner FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (host._owner,)
    db_conn.commit()
    ended = leases.get(
        database,
        event_bus,
        lease["id"],
        local_machine=machine_identity.name(),
    )
    assert ended["status"] == "expired"
    assert ended["rejection_reason"] == "aborted: native runtime restarted"
    spawn.assert_not_called()


# meiva:note
# The little of the compiled graph a terminal handoff touches.
class _HandoffGraph:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.checkpointer = SimpleNamespace()

    async def aget_state(self, _config: object) -> SimpleNamespace:
        return SimpleNamespace(values=dict(self.values))

    async def aupdate_state(self, _config: object, update: dict[str, Any]) -> None:
        self.values.update(update)


# meiva:note
# A restart's replacement admission ends the active automatic lease, and the same wake
# must record its handoff: a silent agent runs no other turn, so without the receipt the
# ended lease keeps `handoff_applied_at` NULL and every later request is refused with
# "unapplied state" — the 18-hour deadlock of #5467.
# meiva:link docs/conventions/agents/agent-impersonation.md
async def test_restarted_host_settles_the_ended_takeover_without_an_external_message(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    from services.agent_runner.agent_host.host import AgentHost

    agent_id = create_agent(db_conn)
    machine = machine_identity.name()
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine) VALUES(%s,'idling',%s)", (agent_id, machine)
    )
    db_conn.commit()
    predecessor = await admit_hosted_runtime(aops_pool, agent_id, machine, uuid4(), db=database)
    assert predecessor is not None
    lease = leases.request(
        database,
        event_bus,
        agent_id,
        caller=CallerIdentity(kind="external_agent", subject="antigravity"),
        process_metadata=recorded_tree(),
        relay_provider="antigravity",
        relay_thread_id=str(uuid4()),
        authority=config_authority,
        name="session-0",
        executor_name="antigravity",
        local_machine=machine_identity.name(),
    )
    leases.prepare(
        database,
        event_bus,
        lease["id"],
        agent_id,
        predecessor,
        "Handoff brief",
        local_machine=machine_identity.name(),
    )
    leases.activate(database, event_bus, lease["id"], predecessor)
    graph = _HandoffGraph()
    host = AgentHost(
        policy=configured_policy(),
        pool=aops_pool,
        checkpointer=MagicMock(),
        graph=cast(Any, graph),
        machine=configured_machine(name=machine),
        bus=EventBus.from_settings(),
        db=Database.from_settings(gate=database_gate),
        catalog=model_catalog,
        commands=CommandCatalog(skills=list),
    )

    await host.run_turn(agent_id)

    ended = leases.get(
        database,
        event_bus,
        lease["id"],
        local_machine=machine_identity.name(),
    )
    assert ended["status"] == "expired"
    assert ended["rejection_reason"] == "aborted: native runtime restarted"
    assert ended["handoff_applied_at"] is not None
    assert Path(ended["handoff_path"]).is_file()
    assert graph.values["impersonation_handoff_id"] == f"{agent_id}:0"
    assert host.stats.turns_started == 0
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("idling",)
    db_conn.commit()

    replacement = leases.request(
        database,
        event_bus,
        agent_id,
        caller=CallerIdentity(kind="external_agent", subject="antigravity"),
        process_metadata=recorded_tree(),
        relay_provider="antigravity",
        relay_thread_id=str(uuid4()),
        authority=config_authority,
        name="session-1",
        executor_name="antigravity",
        local_machine=machine_identity.name(),
    )
    assert replacement["status"] == "requested"


# meiva:note
# Relay supervision ends the lease of a relay child this host watched exit, so the same
# wake owes the same handoff — the second of the two ways a lease wake ends a takeover.
# meiva:link docs/conventions/agents/agent-impersonation.md
async def test_lease_controls_settle_the_handoff_of_a_dead_relays_lease(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    from base.agents.observation.relay_supervision import RelayChild
    from services.agent_runner.agent_host.host import AgentHost

    agent_id = create_agent(db_conn)
    machine = machine_identity.name()
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine) VALUES(%s,'idling',%s)", (agent_id, machine)
    )
    db_conn.commit()
    graph = _HandoffGraph()
    host = AgentHost(
        policy=configured_policy(),
        pool=aops_pool,
        checkpointer=MagicMock(),
        graph=cast(Any, graph),
        machine=configured_machine(name=machine),
        bus=EventBus.from_settings(),
        db=Database.from_settings(gate=database_gate),
        catalog=model_catalog,
        commands=CommandCatalog(skills=list),
    )
    owner = await admit_hosted_runtime(aops_pool, agent_id, machine, host._owner, db=database)
    assert owner is not None
    lease = leases.request(
        database,
        event_bus,
        agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex"),
        process_metadata=recorded_tree(),
        relay_provider="codex",
        relay_thread_id=str(uuid4()),
        authority=config_authority,
        name="session-0",
        executor_name="codex",
        local_machine=machine_identity.name(),
    )
    leases.prepare(
        database,
        event_bus,
        lease["id"],
        agent_id,
        owner,
        "Handoff brief",
        local_machine=machine_identity.name(),
    )
    leases.activate(database, event_bus, lease["id"], owner)
    exited = subprocess.Popen(["/usr/bin/true"])
    exited.wait()
    host.relays.children[agent_id] = RelayChild(
        lease_id=lease["id"], process=exited, token=str(uuid4()), spawned_at=0.0
    )

    await host.run_turn(agent_id)

    ended = leases.get(
        database,
        event_bus,
        lease["id"],
        local_machine=machine_identity.name(),
    )
    assert ended["status"] == "expired"
    assert ended["rejection_reason"] == "aborted: the bound relay process exited (exit code 0)"
    assert ended["handoff_applied_at"] is not None
    assert Path(ended["handoff_path"]).is_file()
    assert graph.values["impersonation_handoff_id"] == f"{agent_id}:0"
    assert agent_id not in host.relays.children


# meiva:note
# One agent whose live automatic takeover the next host wake ends: the setup both
# command paths share, so each test differs only in the lifecycle command it parks.
async def _takeover_in_progress(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    config_authority: ConfigAuthority,
    machine: str,
    *,
    local_machine: str,
) -> tuple[int, str]:
    agent_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine) VALUES(%s,'idling',%s)", (agent_id, machine)
    )
    db_conn.commit()
    predecessor = await admit_hosted_runtime(aops_pool, agent_id, machine, uuid4(), db=database)
    assert predecessor is not None
    lease = leases.request(
        database,
        event_bus,
        agent_id,
        caller=CallerIdentity(kind="external_agent", subject="antigravity"),
        process_metadata=recorded_tree(),
        relay_provider="antigravity",
        relay_thread_id=str(uuid4()),
        authority=config_authority,
        name="session-0",
        executor_name="antigravity",
        local_machine=local_machine,
    )
    leases.prepare(
        database,
        event_bus,
        lease["id"],
        agent_id,
        predecessor,
        "Handoff brief",
        local_machine=local_machine,
    )
    leases.activate(database, event_bus, lease["id"], predecessor)
    return agent_id, cast(str, lease["id"])


def _handoff_host(
    aops_pool: AsyncConnectionPool[Any],
    graph: _HandoffGraph,
    machine: str,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> Any:
    from services.agent_runner.agent_host.host import AgentHost

    return AgentHost(
        policy=configured_policy(),
        pool=aops_pool,
        checkpointer=MagicMock(),
        graph=cast(Any, graph),
        machine=configured_machine(name=machine),
        bus=EventBus.from_settings(),
        db=Database.from_settings(gate=database_gate),
        catalog=model_catalog,
        commands=CommandCatalog(skills=list),
    )


# meiva:note
# A restart command is the common way a takeover ends, and the same wake releases the
# incarnation. The handoff therefore has to be settled before that release: afterwards
# there is no ownership to write the checkpoint under, and the unsettled lease refuses
# every later request. Settling only in the no-command branch left this path (#5492).
# meiva:link docs/conventions/agents/agent-impersonation.md
async def test_restart_command_settles_the_ended_takeover_before_releasing_the_incarnation(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    machine = machine_identity.name()
    agent_id, lease_id = await _takeover_in_progress(
        db_conn,
        aops_pool,
        database,
        event_bus,
        config_authority,
        machine,
        local_machine=machine,
    )
    db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source) VALUES(%s,'','restart','user')",
        (agent_id,),
    )
    db_conn.commit()
    graph = _HandoffGraph()
    host = _handoff_host(aops_pool, graph, machine, model_catalog, database_gate)

    await host.run_turn(agent_id)

    ended = leases.get(database, event_bus, lease_id, local_machine=machine)
    assert ended["status"] == "expired"
    assert ended["rejection_reason"] == "aborted: native runtime restarted"
    assert ended["handoff_applied_at"] is not None
    assert Path(ended["handoff_path"]).is_file()
    assert graph.values["impersonation_handoff_id"] == f"{agent_id}:0"
    assert db_conn.execute(
        "SELECT status,runtime_generation,runtime_owner FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("idling", None, None)
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE agent_id=%s AND kind='restart'", (agent_id,)
    ).fetchone() == ("done",)
    db_conn.commit()

    replacement = leases.request(
        database,
        event_bus,
        agent_id,
        caller=CallerIdentity(kind="external_agent", subject="antigravity"),
        process_metadata=recorded_tree(),
        relay_provider="antigravity",
        relay_thread_id=str(uuid4()),
        authority=config_authority,
        name="session-1",
        executor_name="antigravity",
        local_machine=machine,
    )
    assert replacement["status"] == "requested"


# meiva:note
# A terminate command ends the takeover in its own wake; the handoff still lands before
# the row leaves 'running', so the receipt never waits for whatever revives the agent.
# meiva:link docs/conventions/agents/agent-impersonation.md
async def test_terminate_command_settles_the_ended_takeover_before_the_agent_goes_down(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    machine = machine_identity.name()
    agent_id, lease_id = await _takeover_in_progress(
        db_conn,
        aops_pool,
        database,
        event_bus,
        config_authority,
        machine,
        local_machine=machine,
    )
    db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source) VALUES(%s,'','terminate','user')",
        (agent_id,),
    )
    db_conn.commit()
    graph = _HandoffGraph()
    host = _handoff_host(aops_pool, graph, machine, model_catalog, database_gate)

    await host.run_turn(agent_id)

    ended = leases.get(database, event_bus, lease_id, local_machine=machine)
    assert ended["status"] == "expired"
    assert ended["handoff_applied_at"] is not None
    assert Path(ended["handoff_path"]).is_file()
    assert graph.values["impersonation_handoff_id"] == f"{agent_id}:0"
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("terminated",)
    db_conn.commit()
