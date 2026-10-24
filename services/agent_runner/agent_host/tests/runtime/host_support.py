# meiva:note
# An `AgentHost` over the throwaway database and Redis: agents are real `agents_meta` rows, the
# pools are real, and the only double is the compiled graph, which a turn would otherwise need a
# live model for. The host builds its runtime with the real model factory (inert provider keys
# build a client that never dials), admits, claims and settles through the real ownership code.

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Protocol, cast
from uuid import UUID

import psycopg
import pytest
import pytest_asyncio
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio.client import PubSub

from agent.db import claim_inbound_batch
from base.agents.context import MeivaContext
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.packages.composer_commands import CommandCatalog
from base.paths import meiva_home
from services.agent_runner.agent_host.host import AgentHost
from services.agent_runner.agent_host.runtime import HostPolicy
from services.agent_runner.agent_host.tests.host_policy import configured_policy
from services.agent_runner.agent_host.tests.runtime.observation import (
    HostTurnObservation,
    served_model,
)


def build_agent_host(
    *, catalog: ModelCatalog, database_gate: ProcessDbGate, machine: MachineIdentity, **kwargs: Any
) -> AgentHost:
    """An `AgentHost` on this box with the handles the tests share."""
    return AgentHost(
        home=meiva_home(),
        policy=kwargs.pop("policy", configured_policy()),
        machine=machine,
        bus=kwargs.pop("bus", None) or EventBus.from_settings(),
        db=Database.from_settings(gate=database_gate),
        catalog=catalog,
        **kwargs,
        commands=CommandCatalog.system(skills=list),
    )


class _HostPluginConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    marker: str = Field(default="disk-default", json_schema_extra={"per_agent": True})


@pytest.fixture
def host_plugin() -> dict[str, BaseModel]:
    """One host's plugin config image, passed into its composition root."""
    return {"hostplug": _HostPluginConfig()}


# meiva:note
# ── stored agents ────────────────────────────────────────────────────────────


# meiva:note
# One `agents_meta` row as a host reads it when it wakes.
class Row:
    def __init__(
        self,
        machine: str = "this-box",
        status: str = "running",
        harness: str = "meiva",
        overlay: dict[str, Any] | None = None,
        birth: dict[str, Any] | None = None,
    ) -> None:
        self.machine = machine
        self.status = status
        self.harness = harness
        self.overlay = overlay
        self.birth = birth


# meiva:note
# The rows of one rig, written and read back through a connection of the test's own.
class AgentRows:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def write(self, agent_id: int, row: Row) -> None:
        self._conn.execute(
            "INSERT INTO agents (id) VALUES (%s) ON CONFLICT DO NOTHING", (agent_id,)
        )
        self._conn.execute(
            "INSERT INTO agents_meta (id, machine, status, harness, config_overlay, birth_config) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET "
            "machine=EXCLUDED.machine, status=EXCLUDED.status, "
            "config_overlay=EXCLUDED.config_overlay, birth_config=EXCLUDED.birth_config",
            (
                agent_id,
                row.machine,
                row.status,
                row.harness,
                Jsonb(row.overlay) if row.overlay is not None else None,
                Jsonb(row.birth) if row.birth is not None else None,
            ),
        )
        self._conn.commit()

    def set_overlay(self, agent_id: int, overlay: dict[str, Any] | None) -> None:
        self._conn.execute(
            "UPDATE agents_meta SET config_overlay=%s WHERE id=%s",
            (Jsonb(overlay) if overlay is not None else None, agent_id),
        )
        self._conn.commit()

    def status(self, agent_id: int) -> str:
        row = self._conn.execute(
            "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
        ).fetchone()
        assert row is not None
        return row[0]

    # meiva:note
    # (runtime_owner, runtime_generation) of the agent's row.
    def runtime_ownership(self, agent_id: int) -> tuple[UUID | None, UUID | None]:
        row = self._conn.execute(
            "SELECT runtime_owner, runtime_generation FROM agents_meta WHERE id=%s", (agent_id,)
        ).fetchone()
        assert row is not None
        return row[0], row[1]

    def inbound_status(self, inbound_id: int) -> str:
        row = self._conn.execute(
            "SELECT status FROM inbound_messages WHERE id=%s", (inbound_id,)
        ).fetchone()
        assert row is not None
        return row[0]

    # meiva:note
    # How many database backends wait on a row or transaction lock right now.
    def lock_waiters(self) -> int:
        row = self._conn.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
        ).fetchone()
        assert row is not None
        return row[0]

    def corpse_marked(self, agent_id: int) -> bool:
        row = self._conn.execute(
            "SELECT last_turn_fatal_at IS NOT NULL FROM agents_meta WHERE id=%s", (agent_id,)
        ).fetchone()
        assert row is not None
        return row[0]


# meiva:note
# ── pools ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HostPools:
    workload: AsyncConnectionPool
    control: AsyncConnectionPool


def _pool(db_url: str, *, max_size: int) -> AsyncConnectionPool:
    return AsyncConnectionPool(
        db_url,
        min_size=1,
        max_size=max_size,
        kwargs={"autocommit": True, "prepare_threshold": None},
        check=AsyncConnectionPool.check_connection,
        open=False,
    )


# meiva:note
# The workload pool and the reserved control pool one host runs on.
@pytest_asyncio.fixture
async def host_pools(db_url: str) -> AsyncGenerator[HostPools]:
    async with _pool(db_url, max_size=8) as workload, _pool(db_url, max_size=4) as control:
        await workload.wait()
        await control.wait()
        yield HostPools(workload, control)


# meiva:note
# ── the compiled graph ───────────────────────────────────────────────────────

_IDLE_RESULT: dict[str, Any] = {
    "exit_requested": False,
    "turn_idle": True,
    "restart_requested": False,
}


# meiva:note
# Stands in for the compiled graph: `ainvoke` returns the scripted result of each turn.
#
# It reads the explicit invocation context from inside a child task, where a LangGraph node
# actually runs. A result that requests an exit or a restart first claims the agent's
# lifecycle command in the context's incarnation, as the claim node does before it raises the
# flag.
class ScriptedGraph:
    def __init__(self, results: dict[int, list[dict[str, Any] | BaseException]]) -> None:
        self._results = results
        self.observations: list[HostTurnObservation] = []
        self.gates: dict[int, asyncio.Event] = {}
        self.arrived: dict[int, asyncio.Event] = {}
        self.claim_holds: dict[int, tuple[asyncio.Event, asyncio.Event]] = {}
        self.claiming_chat: set[int] = set()
        self.updates: list[dict[str, Any]] = []

    # meiva:note
    # (claimed, proceed): the turn sets `claimed` once its lifecycle command is claimed and
    # waits for `proceed` before it returns.
    def hold_after_claim(self, agent_id: int) -> tuple[asyncio.Event, asyncio.Event]:
        return self.claim_holds.setdefault(agent_id, (asyncio.Event(), asyncio.Event()))

    def gate(self, agent_id: int) -> asyncio.Event:
        return self.gates.setdefault(agent_id, asyncio.Event())

    def arrival(self, agent_id: int) -> asyncio.Event:
        return self.arrived.setdefault(agent_id, asyncio.Event())

    async def aget_state(self, _config: object, /) -> SimpleNamespace:
        return SimpleNamespace(values={})

    # meiva:note
    # The abort's state write: recorded, as the checkpoint it would land in is not read.
    async def aupdate_state(self, config: object, update: dict[str, Any]) -> object:
        self.updates.append(update)
        return config

    # meiva:note
    # The graph a read view binds to its reconstructing saver: this one, unchanged.
    def copy(self, _update: dict[str, object] | None = None) -> ScriptedGraph:
        return self

    async def _observe(self, agent_id: int, context: MeivaContext) -> HostTurnObservation:
        agent = context.require_agent()
        plugin_cfg = cast(_HostPluginConfig, agent.plugin_config("hostplug"))
        return HostTurnObservation(
            agent_id=context.require_identity().agent_id,
            model=agent.brain.llm_model,
            plugin_marker=plugin_cfg.marker,
            served_model=served_model(context.llm),
            publisher_running=any(
                task.get_name() == f"event-publisher-{agent_id}" for task in asyncio.all_tasks()
            ),
            ops_pool=context.ops_pool,
            machine=context.require_machine(),
        )

    async def ainvoke(
        self, _input: dict[str, Any], /, *, config: dict[str, Any], context: MeivaContext
    ) -> dict[str, Any]:
        agent_id = int(config["configurable"]["thread_id"])
        self.observations.append(await asyncio.create_task(self._observe(agent_id, context)))
        self.arrival(agent_id).set()
        if agent_id in self.claiming_chat:
            # meiva:note
            # The claim node takes the agent's pending chat before the turn works on it.
            assert context.ops_pool is not None
            await claim_inbound_batch(
                context.ops_pool, agent_id, incarnation=context.original_incarnation
            )
        gate = self.gates.get(agent_id)
        if gate is not None:
            await gate.wait()
            gate.clear()
        queued = self._results.get(agent_id)
        result = queued.pop(0) if queued else dict(_IDLE_RESULT)
        if isinstance(result, BaseException):
            raise result
        if result["exit_requested"] or result["restart_requested"]:
            assert context.ops_pool is not None
            await claim_inbound_batch(
                context.ops_pool,
                agent_id,
                lifecycle_only=True,
                incarnation=context.original_incarnation,
            )
            hold = self.claim_holds.get(agent_id)
            if hold is not None:
                hold[0].set()
                await hold[1].wait()
        return result


class GatedGraph:
    """`ainvoke` that blocks on an event, optionally swallowing its own
    cancellation (the C-call-blocked shape the bounded unwind cannot interrupt).
    """

    def __init__(self, *, refuse_cancel: bool = False) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.refuse_cancel = refuse_cancel
        self.cancel_seen = False
        self.contexts: list[MeivaContext] = []

    async def aget_state(self, _config: object, /) -> SimpleNamespace:
        return SimpleNamespace(values={})

    async def ainvoke(
        self, _input: dict[str, Any], /, *, config: dict[str, Any], context: MeivaContext
    ) -> dict[str, Any]:
        self.contexts.append(context)
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancel_seen = True
            if self.refuse_cancel:
                # Model the blocked-in-a-C-call shape: the cancellation lands
                # but the task cannot honour it.
                await self.release.wait()
            raise
        return dict(_IDLE_RESULT)


# meiva:note
# What a host asks of its compiled graph.
class Graph(Protocol):
    async def aget_state(self, config: object, /) -> SimpleNamespace: ...

    async def ainvoke(
        self, input: dict[str, Any], /, *, config: dict[str, Any], context: MeivaContext
    ) -> dict[str, Any]: ...


# meiva:note
# Build a host whose graph is scripted: `results` are each agent's queued turn results.
class Build(Protocol):
    def __call__(
        self,
        rows: dict[int, Row],
        results: dict[int, list[dict[str, Any] | BaseException]] | None = None,
        *,
        catalog: ModelCatalog | None = None,
        policy: HostPolicy | None = None,
        checkpointer: object | None = None,
    ) -> tuple[AgentHost, ScriptedGraph, AgentRows]: ...


# meiva:note
# Build a host over a graph the test made, to drive turns the script cannot.
class BuildWith(Protocol):
    def __call__[G: Graph](
        self,
        rows: dict[int, Row],
        graph: G,
        *,
        catalog: ModelCatalog | None = None,
        policy: HostPolicy | None = None,
        checkpointer: object | None = None,
        workload_pool: AsyncConnectionPool[Any] | None = None,
        control_pool: AsyncConnectionPool[Any] | None = None,
        bus: EventBus | None = None,
    ) -> tuple[AgentHost, G, AgentRows]: ...


# meiva:note
# What `wired` and `wired_with` build over: the pools, the stored rows and the shared handles.
@dataclass(frozen=True)
class Rig:
    pools: HostPools
    stored: AgentRows
    machine: MachineIdentity
    plugins: dict[str, BaseModel]
    catalog: ModelCatalog
    gate: ProcessDbGate

    def host(
        self,
        rows: dict[int, Row],
        graph: object,
        *,
        catalog: ModelCatalog | None,
        policy: HostPolicy | None,
        checkpointer: object | None,
        workload_pool: AsyncConnectionPool[Any] | None,
        control_pool: AsyncConnectionPool[Any] | None,
        bus: EventBus | None,
    ) -> AgentHost:
        for agent_id, row in rows.items():
            self.stored.write(agent_id, row)
        return build_agent_host(
            pool=self.pools.workload if workload_pool is None else workload_pool,
            control_pool=self.pools.control if control_pool is None else control_pool,
            bus=bus,
            checkpointer=object() if checkpointer is None else checkpointer,
            graph=graph,
            plugin_configs=self.plugins,
            catalog=self.catalog if catalog is None else catalog,
            policy=policy if policy is not None else configured_policy(),
            database_gate=self.gate,
            machine=self.machine,
        )


@pytest.fixture
def rig(
    machine_identity: MachineIdentity,
    host_plugin: dict[str, BaseModel],
    model_catalog: ModelCatalog,
    host_pools: HostPools,
    db_conn: psycopg.Connection,
    event_bus: EventBus,
    *,
    database_gate: ProcessDbGate,
) -> Rig:
    machine_identity.set(name="this-box")
    return Rig(
        pools=host_pools,
        stored=AgentRows(db_conn),
        machine=machine_identity,
        plugins=host_plugin,
        catalog=model_catalog,
        gate=database_gate,
    )


# meiva:note
# Build an `AgentHost` over the real pools, with the rows it should find stored.
@pytest.fixture
def wired(rig: Rig) -> Build:

    def _build(
        rows: dict[int, Row],
        results: dict[int, list[dict[str, Any] | BaseException]] | None = None,
        *,
        catalog: ModelCatalog | None = None,
        policy: HostPolicy | None = None,
        checkpointer: object | None = None,
    ) -> tuple[AgentHost, ScriptedGraph, AgentRows]:
        graph = ScriptedGraph(results or {})
        host = rig.host(
            rows,
            graph,
            catalog=catalog,
            policy=policy,
            checkpointer=checkpointer,
            workload_pool=None,
            control_pool=None,
            bus=None,
        )
        return host, graph, rig.stored

    return _build


@pytest.fixture
def wired_with(rig: Rig) -> BuildWith:
    def _build[G: Graph](
        rows: dict[int, Row],
        graph: G,
        *,
        catalog: ModelCatalog | None = None,
        policy: HostPolicy | None = None,
        checkpointer: object | None = None,
        workload_pool: AsyncConnectionPool[Any] | None = None,
        control_pool: AsyncConnectionPool[Any] | None = None,
        bus: EventBus | None = None,
    ) -> tuple[AgentHost, G, AgentRows]:
        host = rig.host(
            rows,
            graph,
            catalog=catalog,
            policy=policy,
            checkpointer=checkpointer,
            workload_pool=workload_pool,
            control_pool=control_pool,
            bus=bus,
        )
        return host, graph, rig.stored

    return _build


# meiva:note
# A real subscriber on the event bus's channel, for the live events a turn published: the
# payloads that arrived since the previous read, whose role matches, in arrival order.
class EventTap:
    def __init__(self, subscription: PubSub) -> None:
        self._subscription = subscription

    async def events(self, role: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        while True:
            # meiva:note
            # The suppression is the harness's: a fixed argv of repository scripts, no shell, no user input.
            message: dict[str, Any] | None = await self._subscription.get_message(  # pyright: ignore[reportUnknownMemberType]
                ignore_subscribe_messages=True, timeout=0.3
            )
            if message is None:
                return found
            event: dict[str, Any] = json.loads(message["data"])
            if event["role"] == role:
                found.append(event)


# meiva:note
# The tap subscribes before the test starts any turn. The redis stubs leave `pubsub` and
# `get_message` untyped.
@pytest_asyncio.fixture
async def event_tap(event_bus: EventBus) -> AsyncGenerator[EventTap]:
    client = event_bus.open_async_redis()
    async with client.pubsub() as subscription:  # pyright: ignore[reportUnknownMemberType]
        await subscription.subscribe(event_bus.channel)
        await subscription.get_message(timeout=1.0)  # pyright: ignore[reportUnknownMemberType]
        yield EventTap(subscription)
    await client.aclose()


__all__ = ["GatedGraph"]
