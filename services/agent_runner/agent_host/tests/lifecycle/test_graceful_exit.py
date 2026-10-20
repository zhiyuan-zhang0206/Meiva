# meiva:note
# A SIGTERMed agent-host leaves each running turn a durable restart, on real Postgres. The
# turns run the production graph, claim node, settlement and checkpointer; only the model is
# a test double, reached through the ordinary `llm_override` seam.
# meiva:link services/agent_runner/agent_host/lifecycle/docs/graceful-exit.meiva.okf.md

import asyncio
import dataclasses
import json
import signal
import subprocess
import sys
from functools import partial
from typing import Any

import psycopg
import pytest
from langchain_core.messages import BaseMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from agent.graph import build_graph
from base.agents.inbox.insert import insert_inbound_message
from base.agents.observation.turn_progress import TurnProgress
from base.agents.tests.units import spawn_agent
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.log.tests.log_records import LogRecords
from base.packages.composer_commands import CommandCatalog
from base.packages.plugins.extensions import ExtensionRegistry
from services.agent_runner.agent_host.daemon import build_checkpointer
from services.agent_runner.agent_host.dispatcher import TurnScheduler
from services.agent_runner.agent_host.host import AgentHost
from services.agent_runner.agent_host.lifecycle.graceful_exit import (
    RESTART_SOURCE,
    GracefulExit,
    OwedRestart,
    write_exit_restart,
)
from services.agent_runner.agent_host.tests.host_policy import configured_policy
from services.agent_runner.agent_host.tests.lifecycle import blocking_model


async def _dispatch_forever() -> None:
    await asyncio.Event().wait()


@dataclasses.dataclass
class _Host:
    host: AgentHost
    scheduler: TurnScheduler
    graceful: GracefulExit
    checkpointer: AsyncPostgresSaver

    def serve(self) -> asyncio.Task[None]:
        return asyncio.create_task(self.graceful.serve(_dispatch_forever()))

    async def close(self) -> None:
        await asyncio.wait_for(self.scheduler.aclose(), 30)
        await asyncio.wait_for(self.host.aclose(), 30)

    async def messages(self, agent_id: int) -> list[BaseMessage]:
        saved = await self.checkpointer.aget_tuple({"configurable": {"thread_id": str(agent_id)}})
        assert saved is not None
        return list(saved.checkpoint["channel_values"]["messages"])


@dataclasses.dataclass
class _Cluster:
    conn: psycopg.Connection[Any]
    pool: AsyncConnectionPool
    database: Database
    bus: EventBus
    catalog: ModelCatalog
    authority: ConfigAuthority
    gate: ProcessDbGate
    machine: MachineIdentity

    def spawn(self, *, chat: bool) -> tuple[int, blocking_model.Gate]:
        agent_id = spawn_agent(
            machine=self.machine,
            catalog=self.catalog,
            authority=self.authority,
            database_gate=self.gate,
        )
        if chat:
            insert_inbound_message(
                self.conn, agent_id, "hello", source="user", database=self.database, bus=self.bus
            )
        gate = blocking_model.GATES[agent_id] = blocking_model.Gate()
        return agent_id, gate

    def restarts(self, agent_id: int) -> list[tuple[Any, ...]]:
        self.conn.commit()
        return self.conn.execute(
            "SELECT id,status FROM inbound_messages "
            "WHERE agent_id=%s AND kind='restart' AND source=%s ORDER BY id",
            (agent_id, RESTART_SOURCE),
        ).fetchall()

    def row(self, agent_id: int) -> tuple[Any, ...]:
        self.conn.commit()
        row = self.conn.execute(
            "SELECT status,runtime_owner FROM agents_meta WHERE id=%s", (agent_id,)
        ).fetchone()
        assert row is not None
        return row

    async def host(self, *, drain_s: float) -> _Host:
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
                configured_policy(), llm_override=lambda: blocking_model.OVERRIDE
            ),
            commands=CommandCatalog(skills=list),
        )
        scheduler = TurnScheduler(host.run_turn)
        graceful = GracefulExit(
            turns=scheduler,
            progress=host.turn_progress,
            write_restart=partial(write_exit_restart, self.database, self.bus),
            drain_s=drain_s,
        )
        return _Host(host, scheduler, graceful, checkpointer)

    async def in_flight_turn(self, *, drain_s: float) -> tuple[int, blocking_model.Gate, _Host]:
        agent_id, gate = self.spawn(chat=True)
        served = await self.host(drain_s=drain_s)
        served.scheduler.wake(agent_id)
        await asyncio.wait_for(gate.entered.wait(), 30)
        return agent_id, gate, served


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
) -> _Cluster:
    return _Cluster(
        db_conn,
        aops_pool,
        database,
        event_bus,
        model_catalog,
        config_authority,
        database_gate,
        machine_identity,
    )


class _Turns:
    def __init__(self, tasks: dict[int, asyncio.Task[None]]) -> None:
        self._tasks = tasks

    def task_for(self, agent_id: int) -> asyncio.Task[None] | None:
        return self._tasks.get(agent_id)

    def stop_admission(self) -> frozenset[int]:
        return frozenset(self._tasks)


async def _drain_with(
    write_restart: Any, *, drain_s: float
) -> tuple[dict[int, asyncio.Task[None]], float]:
    tasks = {agent: asyncio.create_task(_dispatch_forever()) for agent in (1, 2)}
    graceful = GracefulExit(
        turns=_Turns(tasks), progress=TurnProgress(), write_restart=write_restart, drain_s=drain_s
    )
    graceful.request()
    started = asyncio.get_running_loop().time()
    try:
        await asyncio.wait_for(graceful.drain(), drain_s + 5)
    finally:
        for task in tasks.values():
            task.cancel()
    return tasks, asyncio.get_running_loop().time() - started


async def test_a_failed_restart_write_is_named_and_the_turn_still_waited_for(
    loguru_records: LogRecords,
) -> None:
    async def write_restart(agent_id: int) -> OwedRestart:
        if agent_id == 1:
            raise psycopg.OperationalError("database went away")
        return OwedRestart(20, written=True)

    await _drain_with(write_restart, drain_s=0.2)
    [failed] = loguru_records.events("host_exit_restart_failed")
    assert failed["level"].name == "ERROR" and failed["extra"]["agent_id"] == 1
    assert "database went away" in failed["extra"]["reason"]
    assert {
        r["extra"]["agent_id"]: r["extra"]["restart"]
        for r in loguru_records.events("host_exit_drain_timeout")
    } == {1: None, 2: 20}


async def test_restart_writes_share_the_drain_deadline(loguru_records: LogRecords) -> None:
    async def write_restart(agent_id: int) -> OwedRestart:
        await asyncio.Event().wait()
        raise AssertionError(f"the write for agent {agent_id} never returns")

    _, elapsed = await _drain_with(write_restart, drain_s=0.3)
    assert elapsed < 2
    assert sorted(
        r["extra"]["agent_id"] for r in loguru_records.events("host_exit_restart_failed")
    ) == [1, 2]
    assert all(
        r["extra"]["reason"] == "the drain deadline passed first"
        for r in loguru_records.events("host_exit_restart_failed")
    )
    assert len(loguru_records.events("host_exit_drain_timeout")) == 2


async def test_an_idle_agent_gets_no_restart(cluster: _Cluster, loguru_records: LogRecords) -> None:
    agent_id, gate = cluster.spawn(chat=False)
    served = await cluster.host(drain_s=30)
    serving = served.serve()
    served.graceful.request()
    await asyncio.wait_for(serving, 5)
    await served.close()

    assert cluster.restarts(agent_id) == []
    assert gate.calls == 0
    assert cluster.row(agent_id) == ("idling", None)
    assert [r["extra"]["written"] for r in loguru_records.events("host_exit_draining")] == [0]
    assert len(loguru_records.events("host_exit_drained")) == 1


def test_a_real_sigterm_drains_instead_of_interrupting_and_a_second_one_is_ignored() -> None:
    child = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "services.agent_runner.agent_host.tests.lifecycle.child_graceful_exit",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ready"
        child.send_signal(signal.SIGTERM)
        assert child.stdout.readline().strip() == "draining"
        child.send_signal(signal.SIGTERM)
        output, errors = child.communicate(timeout=30)
    finally:
        child.kill()
    assert child.returncode == 0, errors
    assert json.loads(output.strip().splitlines()[-1]) == {
        "restarts_for": [7],
        "turn_settled": True,
        "repeats": 1,
    }
