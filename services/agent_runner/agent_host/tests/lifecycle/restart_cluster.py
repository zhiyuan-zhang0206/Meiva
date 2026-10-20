# meiva:note
# A small cluster for the restart tests, on real Postgres: hosts that run the production graph,
# claim node, settlement and checkpointer over a scripted model, and the rows a test reads and
# ages. A restart's interrupt grace is read from the restart row's own `created_at`, so a test
# ages that row instead of waiting out the grace.
# meiva:link services/agent_runner/agent_host/lifecycle/docs/graceful-exit.meiva.okf.md

from __future__ import annotations

import asyncio
import dataclasses
from functools import partial
from typing import Any

import psycopg
import pytest
from langchain_core.messages import BaseMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from agent.graph import build_graph
from base.agents.context.clients import ClientSet
from base.agents.inbox.insert import insert_inbound_message
from base.agents.tests.units import spawn_agent
from base.cluster.machine import MachineIdentity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.native_process.stop_timing import RESTART_INTERRUPT_GRACE_S
from base.packages.composer_commands import CommandCatalog
from base.packages.plugins.extensions import ExtensionRegistry
from services.agent_runner.agent_host.daemon import build_checkpointer
from services.agent_runner.agent_host.dispatcher import TurnScheduler
from services.agent_runner.agent_host.host import AgentHost
from services.agent_runner.agent_host.lifecycle.graceful_exit import (
    GracefulExit,
    write_exit_restart,
)
from services.agent_runner.agent_host.scheduling.pending_wakes import scan_rows
from services.agent_runner.agent_host.tests.host_policy import configured_policy
from services.agent_runner.agent_host.tests.lifecycle import scripted_model

__all__ = ["Cluster", "Host", "Inbound", "cluster"]


async def dispatch_forever() -> None:
    await asyncio.Event().wait()


@dataclasses.dataclass
class Host:
    host: AgentHost
    scheduler: TurnScheduler
    graceful: GracefulExit
    checkpointer: AsyncPostgresSaver

    def serve(self) -> asyncio.Task[None]:
        return asyncio.create_task(self.graceful.serve(dispatch_forever()))

    def start_turn(self, agent_id: int) -> asyncio.Task[None]:
        return asyncio.create_task(self.host.run_turn(agent_id))

    async def close(self) -> None:
        await asyncio.wait_for(self.scheduler.aclose(), 30)
        await asyncio.wait_for(self.host.aclose(), 30)

    async def messages(self, agent_id: int) -> list[BaseMessage]:
        saved = await self.checkpointer.aget_tuple({"configurable": {"thread_id": str(agent_id)}})
        assert saved is not None
        return list(saved.checkpoint["channel_values"]["messages"])


@dataclasses.dataclass(frozen=True)
class Inbound:
    id: int
    kind: str
    source: str
    status: str
    content: str


@dataclasses.dataclass
class Cluster:
    conn: psycopg.Connection[Any]
    pool: AsyncConnectionPool
    database: Database
    bus: EventBus
    catalog: ModelCatalog
    authority: ConfigAuthority
    gate: ProcessDbGate
    machine: MachineIdentity

    def spawn(
        self, *, chat: str | None, steps: list[scripted_model.Step] | None = None
    ) -> tuple[int, scripted_model.Script]:
        agent_id = spawn_agent(
            machine=self.machine,
            catalog=self.catalog,
            authority=self.authority,
            database_gate=self.gate,
        )
        if chat is not None:
            self.send(agent_id, chat)
        script = scripted_model.SCRIPTS[agent_id] = (
            scripted_model.Script() if steps is None else scripted_model.Script(steps=steps)
        )
        return agent_id, script

    def send(self, agent_id: int, content: str, *, kind: str = "chat", source: str = "user") -> int:
        return insert_inbound_message(
            self.conn,
            agent_id,
            content,
            source=source,
            kind=kind,
            database=self.database,
            bus=self.bus,
        )

    # meiva:note
    # Make a pending command look as if it had waited `seconds` already.
    def age(self, inbound_id: int, seconds: float) -> None:
        self.conn.execute(
            "UPDATE inbound_messages SET created_at = clock_timestamp() - make_interval(secs => %s) "
            "WHERE id=%s",
            (seconds, inbound_id),
        )
        self.conn.commit()

    # meiva:note
    # Age a pending restart past its interrupt grace, as if it had waited that long.
    def let_grace_pass(self, inbound_id: int) -> None:
        self.age(inbound_id, RESTART_INTERRUPT_GRACE_S + 1)

    def inbound(self, agent_id: int, *kinds: str) -> list[Inbound]:
        self.conn.commit()
        rows = self.conn.execute(
            "SELECT id,kind,source,status,content FROM inbound_messages "
            "WHERE agent_id=%s AND kind=ANY(%s) ORDER BY id",
            (agent_id, list(kinds)),
        ).fetchall()
        return [Inbound(*row) for row in rows]

    def row(self, agent_id: int) -> tuple[str, Any]:
        self.conn.commit()
        row = self.conn.execute(
            "SELECT status,runtime_owner FROM agents_meta WHERE id=%s", (agent_id,)
        ).fetchone()
        assert row is not None
        return row

    async def woken_by_a_new_host(self, agent_id: int) -> bool:
        return agent_id in await scan_rows(self.pool, self.machine.name())

    async def host(self, *, drain_s: float = 30) -> Host:
        checkpointer = await build_checkpointer(self.pool, read_checkpoint_interval=lambda: 1)
        host = AgentHost(
            pool=self.pool,
            checkpointer=checkpointer,
            graph=build_graph(checkpointer, ExtensionRegistry()),
            machine=self.machine,
            bus=self.bus,
            db=self.database,
            catalog=self.catalog,
            policy=dataclasses.replace(
                configured_policy(), llm_override=lambda: scripted_model.OVERRIDE
            ),
            commands=CommandCatalog(skills=list),
            # meiva:note
            # The exec child's request names a gateway; the tests' code never calls it.
            clients=ClientSet(
                database=lambda: self.database, gateway_url="http://gateway.invalid:8000"
            ),
        )
        scheduler = TurnScheduler(host.run_turn)
        graceful = GracefulExit(
            turns=scheduler,
            progress=host.turn_progress,
            write_restart=partial(write_exit_restart, self.database, self.bus),
            drain_s=drain_s,
        )
        return Host(host, scheduler, graceful, checkpointer)


@pytest.fixture
def cluster(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> Cluster:
    return Cluster(
        db_conn,
        aops_pool,
        database,
        event_bus,
        model_catalog,
        config_authority,
        database_gate,
        machine_identity,
    )
