from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from psycopg_pool import AsyncConnectionPool

from agent.state import BaseAgentState
from agent.takeover.process import spawn_relay
from agent.takeover.relay import RelayBinding, RelayWait, TakeoverHost
from base.agents import impersonation as leases
from base.agents.context import MeivaContext
from base.agents.impersonation.tests.impersonation_support import recorded_tree
from base.agents.observation.relay_supervision import RelaySupervision
from base.api_contracts.caller_identity import CallerIdentity
from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from base.cluster.machine import MachineIdentity
from base.config.service_read import ConfigAuthority
from base.config.tests.configuration import defaults
from base.db import Database, create_agent
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.packages.composer_commands import CommandCatalog
from base.packages.plugin_providers import build_model_catalog

__all__ = [
    "QUICK_WAIT",
    "Cluster",
    "cluster",
    "exited_child",
    "fixed_clock",
    "idle_graph",
    "native_context",
    "relay_program",
    "sleeper",
    "stub_host",
]

RELAY_STUB = Path(__file__).with_name("relay_stub.py")
QUICK_WAIT = RelayWait(timeout_s=30.0, poll_s=0.05)
STUB_CREDENTIAL = "stub-credential"
LEASE_PHASES = ("requested", "accepted", "active", "released", "expired")


def fixed_clock(*, weekday: bool = False) -> Clock:
    return Clock(ClockConfig("UTC", "UTC", weekday), timers=SYSTEM_TIMERS)


@dataclass(frozen=True)
class Cluster:
    db_conn: psycopg.Connection[Any]
    database: Database
    event_bus: EventBus
    authority: ConfigAuthority
    machine: MachineIdentity

    @property
    def local_machine(self) -> str:
        return self.machine.name()

    def native_agent(self) -> RuntimeIncarnation:
        agent_id = create_agent(self.db_conn)
        owner = RuntimeIncarnation(agent_id, uuid4(), uuid4())
        self.db_conn.execute(
            "INSERT INTO agents_meta(id,status,machine,runtime_generation,runtime_owner) "
            "VALUES(%s,'idling',%s,%s,%s)",
            (agent_id, self.local_machine, owner.generation, owner.owner),
        )
        self.db_conn.commit()
        return owner

    def lease(
        self,
        owner: RuntimeIncarnation,
        *,
        provider: str = "codex",
        phase: str = "accepted",
        codex_remote: str | None = None,
    ) -> dict[str, Any]:
        if phase not in LEASE_PHASES:
            raise ValueError(f"unknown lease phase {phase!r}")
        session = leases.request(
            self.database,
            self.event_bus,
            owner.agent_id,
            caller=CallerIdentity(kind="external_agent", subject=provider, instance="test"),
            ttl_seconds=3600,
            reason="Finish the assigned task",
            process_metadata=recorded_tree(),
            relay_provider=provider,
            relay_thread_id=str(uuid4()) if provider in ("codex", "antigravity") else None,
            relay_codex_remote=codex_remote,
            authority=self.authority,
            local_machine=self.local_machine,
            name="Test name",
            executor_name="Test executor_name",
        )
        if phase == "requested":
            return session
        leases.prepare(
            self.database,
            self.event_bus,
            session["id"],
            owner.agent_id,
            owner,
            "Handoff brief",
            local_machine=self.local_machine,
        )
        if phase != "accepted":
            leases.activate(self.database, self.event_bus, session["id"], owner)
        if phase == "released":
            leases.release(
                self.database,
                self.event_bus,
                session["id"],
                "Done",
                local_machine=self.local_machine,
            )
        if phase == "expired":
            self.db_conn.execute(
                "UPDATE agent_impersonations SET expires_at=clock_timestamp()-interval '1 second' "
                "WHERE id=%s",
                (session["id"],),
            )
            self.db_conn.commit()
        return self.get(session["id"])

    def get(self, lease_id: str) -> dict[str, Any]:
        return leases.get(self.database, self.event_bus, lease_id, local_machine=self.local_machine)

    def age_relay(self, lease_id: str, *, seconds: float) -> None:
        self.db_conn.execute(
            "UPDATE agent_impersonations SET relay_heartbeat_at="
            "clock_timestamp() - %s::float8 * interval '1 second', relay_minted_at="
            "clock_timestamp() - %s::float8 * interval '1 second' WHERE id=%s",
            (seconds, seconds, lease_id),
        )
        self.db_conn.commit()

    def status(self, owner: RuntimeIncarnation) -> dict[str, Any] | None:
        return leases.native_status(self.database, self.event_bus, owner.agent_id, owner)

    def heartbeat(self, lease_id: str, *, seconds_ago: float | None) -> None:
        self.db_conn.execute(
            "UPDATE agent_impersonations SET relay_heartbeat_at="
            "CASE WHEN %s::float8 IS NULL THEN NULL "
            "ELSE clock_timestamp() - %s::float8 * interval '1 second' END WHERE id=%s",
            (seconds_ago, seconds_ago, lease_id),
        )
        self.db_conn.commit()


def native_context(
    cluster: Cluster, owner: RuntimeIncarnation, *, ops_pool: AsyncConnectionPool[Any] | None = None
) -> MeivaContext:
    return MeivaContext(
        db=cluster.database,
        bus=cluster.event_bus,
        machine=cluster.machine,
        relays=RelaySupervision(),
        catalog=build_model_catalog(),
        clock_factory=fixed_clock,
        agent=AgentSlices.resolve(default_reader=defaults()),
        commands=CommandCatalog.system(skills=list),
        ops_pool=ops_pool,
        original_incarnation=owner,
    )


async def idle_graph(agent_id: int) -> tuple[Any, RunnableConfig]:
    builder: Any = StateGraph(BaseAgentState)

    def idle(_state: BaseAgentState) -> dict[str, Any]:
        return {}

    builder.add_node("idle", idle)
    builder.add_edge(START, "idle")
    builder.add_edge("idle", END)
    graph: Any = builder.compile(checkpointer=MemorySaver())
    config: RunnableConfig = {"configurable": {"thread_id": str(agent_id)}}
    await graph.ainvoke(
        {"messages": [HumanMessage(id="initial-request", content="Finish the task")]}, config
    )
    return graph, config


@pytest.fixture
def cluster(
    db_conn: psycopg.Connection[Any],
    database: Database,
    event_bus: EventBus,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> Cluster:
    return Cluster(db_conn, database, event_bus, config_authority, machine_identity)


def relay_program(directory: Path, mode: str) -> tuple[str, ...]:
    directory.mkdir(exist_ok=True)
    return (sys.executable, "-I", str(RELAY_STUB), str(directory), mode)


def stub_host(
    cluster: Cluster, tmp_path: Path, mode: str, *, wait: RelayWait = QUICK_WAIT
) -> TakeoverHost:
    return TakeoverHost(
        local_machine=cluster.local_machine,
        workspaces_root=tmp_path / "workspaces",
        relay_program=relay_program(tmp_path / "relay", mode),
        wait=wait,
    )


def start_stub(directory: Path, mode: str) -> subprocess.Popen[bytes]:
    binding = RelayBinding(
        agent_id=1,
        lease_id="stub-lease",
        provider="codex",
        thread_id="stub-thread",
        codex_remote=None,
        antigravity_endpoint_file=None,
        codex_home=None,
    )
    return spawn_relay(relay_program(directory, mode), binding, token=STUB_CREDENTIAL)


def exited_child(directory: Path) -> subprocess.Popen[bytes]:
    child = start_stub(directory, "record")
    child.wait(timeout=10)
    return child


@pytest.fixture
def sleeper(tmp_path: Path) -> Iterator[subprocess.Popen[bytes]]:
    process = start_stub(tmp_path / "sleeper", "serve")
    try:
        yield process
    finally:
        process.kill()
        process.wait()
