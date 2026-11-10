# meiva:note
# The agent's own restart while an external harness turn runs: the command is recorded (source
# `self`) and waits. The harness turn is not interrupted however long it waits, messages that
# arrive behind it are not steered into the running turn (the command is a barrier), and when the
# turn returns the claim takes the command alone, so everything behind it stays pending in
# order for the harness the restart leaves the agent on.
# meiva:link agent/graph/docs/harness-turn.md

from __future__ import annotations

import asyncio
from dataclasses import replace

import psycopg
from psycopg_pool import AsyncConnectionPool

from agent.db import claim_inbound_batch
from agent.graph.claim.harness_turn import harness_turn_node
from agent.graph.claim.tests.test_harness_turn import FakeEngine, config, deliver, runtime
from agent.graph.claim.tests.test_harness_turn import hosted_resources as hosted_resources
from agent.ownership.tests.ownership_support import admit_owner, agent_row
from agent.state import AgentState
from base.db.code_version_gate import ProcessDbGate
from base.lm.harness import AssistantText, ForegroundAccepted, HarnessTurn
from base.lm.harness.engine import TurnEventSink
from base.native_process.stop_timing import INTERRUPT_POLL_S
from base.native_process.turn_identity import HostedTurnResources


class _Working(FakeEngine):
    def __init__(self) -> None:
        super().__init__()
        self.running = asyncio.Event()
        self.finish = asyncio.Event()
        self.steering_available = True
        self.steered: list[str] = []

    async def run_turn(
        self, text: str, *, key: str, on_event: TurnEventSink | None = None
    ) -> HarnessTurn:
        assert on_event is not None
        await deliver(on_event, ForegroundAccepted(turn_id="t1"))
        self.running.set()
        await self.finish.wait()
        await deliver(on_event, AssistantText(turn_id="t1", text="Finished the work"))
        self.steering_available = False
        return replace(await super().run_turn(text, key=key), interrupted=False)

    async def steer(self, text: str, *, key: str) -> bool:
        self.steered.append(text)
        return True


def _insert(db_conn: psycopg.Connection, agent_id: int, kind: str, source: str, text: str) -> int:
    row = db_conn.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (agent_id, text, kind, source),
    ).fetchone()
    db_conn.commit()
    assert row is not None
    return row[0]


def _status(db_conn: psycopg.Connection, inbound_id: int) -> object:
    db_conn.commit()
    row = db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (inbound_id,)
    ).fetchone()
    assert row is not None
    return row[0]


async def test_the_running_turn_is_neither_interrupted_nor_steered_and_the_command_is_claimed_alone(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    engine = _Working()
    state = AgentState(harness_pending_input={"prompt": "work", "key": "initial"})
    rt = runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources)

    async with asyncio.timeout(30), asyncio.TaskGroup() as tasks:
        turn = tasks.create_task(harness_turn_node(state, rt, config(agent_id)))
        await engine.running.wait()
        restart = _insert(db_conn, agent_id, "restart", "self", "I need a real shell")
        # meiva:note
        # Aged past the grace a peer's restart would be interrupted at: the agent's own never is.
        db_conn.execute(
            "UPDATE inbound_messages SET created_at = clock_timestamp() - interval '1 hour' "
            "WHERE id = %s",
            (restart,),
        )
        db_conn.commit()
        behind = [
            _insert(db_conn, agent_id, "chat", "user", "first after the request"),
            _insert(db_conn, agent_id, "chat", "user", "second after the request"),
        ]
        await asyncio.sleep(INTERRUPT_POLL_S * 2 + 0.5)
        assert not turn.done()
        assert engine.interrupts == 0
        assert engine.steered == []
        assert [_status(db_conn, i) for i in (restart, *behind)] == ["pending"] * 3
        engine.finish.set()

    # meiva:note
    # The turn returned on its own. The next claim takes the command first and by itself; the
    # messages behind it are untouched, in the order they arrived, for the harness the restart
    # leaves the agent on.
    assert engine.interrupts == 0
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    [claimed] = await claim_inbound_batch(aops_pool, agent_id, incarnation=owner)
    assert (claimed.id, claimed.kind, claimed.durable_lifecycle) == (restart, "restart", True)
    assert [_status(db_conn, i) for i in behind] == ["pending", "pending"]
