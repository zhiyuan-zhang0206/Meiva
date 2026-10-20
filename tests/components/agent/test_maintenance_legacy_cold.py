"""Real persisted ENDs distinguish retired consumers from unfinished work."""

import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypedDict
from uuid import uuid4

import psycopg
import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from psycopg_pool import AsyncConnectionPool

from agent.ownership.hosted import admit_hosted_runtime, settle_hosted_runtime
from base.agents.inbox.insert import insert_inbound_message
from base.cluster.machine import MachineIdentity
from base.db import Database
from base.deploy.maintenance import cohort, pause_owner
from base.deploy.tests.maintenance_factory import WHEN, maintenance_agent
from base.deploy.tests.maintenance_factory import isolate as isolate
from base.events.live.bus import EventBus
from ops.agent_pause import resume_agents


class _State(TypedDict):
    halted: bool
    exit_requested: bool
    restart_requested: bool
    history: list[str]


def _persist_end(conn: psycopg.Connection[Any], agent: int, *, pending: str | None = None) -> None:
    def claim(_state: _State) -> Command[Any]:
        return Command(
            update={"halted": True, "exit_requested": False, "restart_requested": False},
            goto=END if pending is None else "work",
        )

    def work(_state: _State) -> Command[Any]:
        interrupt("The action still needs input")
        return Command(goto=END)

    saver = PostgresSaver(conn)
    builder: Any = StateGraph(_State)
    builder.add_node("claim", claim)
    builder.add_node("work", work)
    builder.add_edge(START, "claim")
    graph = builder.compile(
        checkpointer=saver, interrupt_before=["work"] if pending == "branch" else []
    )
    config: RunnableConfig = {"configurable": {"thread_id": str(agent)}}
    graph.invoke(
        {
            "halted": False,
            "exit_requested": False,
            "restart_requested": False,
            "history": ["Keep the original context"],
        },
        config,
    )
    snapshot = graph.get_state(config)
    if pending is None:
        assert snapshot.next == () and snapshot.tasks == ()
    else:
        assert snapshot.next == ("work",) and snapshot.tasks
        if pending == "interrupt":
            assert snapshot.tasks[0].interrupts
    conn.commit()


def _retired(conn: psycopg.Connection[Any], *, machine_identity: MachineIdentity) -> int:
    agent = maintenance_agent(conn, machine_name=machine_identity.name())
    conn.execute(
        "UPDATE agents_meta SET status='idling',runtime_kind='hosted',runtime_owner=%s,"
        "runtime_generation=%s,lease_expires_at=%s WHERE id=%s",
        (uuid4(), uuid4(), datetime.now(UTC) - timedelta(minutes=5), agent),
    )
    conn.commit()
    _persist_end(conn, agent)
    return agent


def test_completed_retired_consumer_parks_without_inventing_receipts(
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_identity: MachineIdentity,
) -> None:
    agent = _retired(db_conn, machine_identity=machine_identity)
    before = db_conn.execute(
        "SELECT runtime_kind,runtime_owner,runtime_generation,lease_expires_at,"
        "lifecycle_command_id,incarnation_resources FROM agents_meta WHERE id=%s",
        (agent,),
    ).fetchone()
    messages = db_conn.execute(
        "SELECT * FROM inbound_messages WHERE agent_id=%s ORDER BY id", (agent,)
    ).fetchall()
    checkpoints = db_conn.execute(
        "SELECT * FROM checkpoints WHERE thread_id=%s ORDER BY checkpoint_id", (str(agent),)
    ).fetchall()
    db_conn.commit()
    pause_owner.begin_maintenance("cold", WHEN)
    hold = cohort.prepare(
        db_conn,
        machine=machine_identity.name(),
        host_owner=None,
        holder="cold",
        acquired_at=WHEN,
        host_absent=True,
    )
    assert hold.parked == (agent,)  # time-bomb-ok: WHEN is hold identity; this compares agent IDs.
    assert not hold.commands and not hold.drained
    cohort.verify_drained(db_conn, hold)
    assert db_conn.execute("SELECT status FROM agents_meta WHERE id=%s", (agent,)).fetchone() == (
        "idling",
    )
    assert (
        db_conn.execute(
            "SELECT runtime_kind,runtime_owner,runtime_generation,lease_expires_at,"
            "lifecycle_command_id,incarnation_resources FROM agents_meta WHERE id=%s",
            (agent,),
        ).fetchone()
        == before
    )
    assert (
        db_conn.execute(
            "SELECT * FROM inbound_messages WHERE agent_id=%s ORDER BY id", (agent,)
        ).fetchall()
        == messages
    )
    assert (
        db_conn.execute(
            "SELECT * FROM checkpoints WHERE thread_id=%s ORDER BY checkpoint_id", (str(agent),)
        ).fetchall()
        == checkpoints
    )


def _prepare(conn: psycopg.Connection[Any], *, machine_identity: MachineIdentity) -> None:
    pause_owner.begin_maintenance("cold", WHEN)
    cohort.prepare(
        conn,
        machine=machine_identity.name(),
        host_owner=None,
        holder="cold",
        acquired_at=WHEN,
        host_absent=True,
    )


@pytest.mark.parametrize(
    "defect", ["fresh_lease", "unknown_version", "branch", "interrupt", "no_end"]
)
def test_incomplete_or_live_end_is_not_parked(
    db_conn: psycopg.Connection[Any], defect: str, *, machine_identity: MachineIdentity
) -> None:
    agent = _retired(db_conn, machine_identity=machine_identity)
    if defect == "fresh_lease":
        db_conn.execute(
            "UPDATE agents_meta SET lease_expires_at=clock_timestamp()+interval '1 hour' "
            "WHERE id=%s",
            (agent,),
        )
    elif defect == "unknown_version":
        db_conn.execute(
            "UPDATE checkpoints SET checkpoint=jsonb_set(checkpoint,'{v}','99') WHERE thread_id=%s",
            (str(agent),),
        )
    elif defect == "no_end":
        db_conn.execute("DELETE FROM checkpoints WHERE thread_id=%s", (str(agent),))
    else:
        _persist_end(db_conn, agent, pending=defect)
    db_conn.commit()
    before = db_conn.execute("SELECT * FROM agents_meta WHERE id=%s", (agent,)).fetchone()
    db_conn.commit()
    with pytest.raises(RuntimeError):
        _prepare(db_conn, machine_identity=machine_identity)
    assert db_conn.execute("SELECT * FROM agents_meta WHERE id=%s", (agent,)).fetchone() == before


@pytest.mark.parametrize("kind", ["restart", "terminate"])
def test_pending_lifecycle_keeps_the_original_cold_intent(
    db_conn: psycopg.Connection[Any],
    kind: str,
    database: Database,
    event_bus: EventBus,
    *,
    machine_identity: MachineIdentity,
) -> None:
    agent = _retired(db_conn, machine_identity=machine_identity)
    insert_inbound_message(db_conn, agent, "", "user", kind=kind, bus=event_bus, database=database)
    before = db_conn.execute("SELECT * FROM agents_meta WHERE id=%s", (agent,)).fetchone()
    db_conn.commit()
    with pytest.raises(RuntimeError):
        _prepare(db_conn, machine_identity=machine_identity)
    assert db_conn.execute("SELECT * FROM agents_meta WHERE id=%s", (agent,)).fetchone() == before


def test_real_unrecorded_legacy_consumer_blocks_cold_prepare(
    db_conn: psycopg.Connection[Any], tmp_path: Path, *, machine_identity: MachineIdentity
) -> None:
    agent = _retired(db_conn, machine_identity=machine_identity)
    package = tmp_path / "agent"
    package.mkdir()
    (package / "__init__.py").write_text("")
    ready = tmp_path / "ready"
    (package / "__main__.py").write_text(
        "from pathlib import Path\nimport time\nPath('ready').touch()\ntime.sleep(30)\n"
    )
    # This private package exercises the real -m agent launch shape without
    # importing the framework or contacting any production cluster.
    with subprocess.Popen(  # noqa: S603 — fixed Python fixture module under tmp_path
        [sys.executable, "-m", "agent", "--agent-id", str(agent)],
        cwd=tmp_path,
        env=dict(os.environ),
    ) as process:
        try:
            deadline = time.monotonic() + 5
            while not ready.exists():
                assert process.poll() is None and time.monotonic() < deadline
                time.sleep(0.02)
            with pytest.raises(RuntimeError, match="native consumer"):
                _prepare(db_conn, machine_identity=machine_identity)
        finally:
            # SIGKILL: SIGTERM=SIG_IGN inherited from the session shell
            # would leave the child alive and hang this teardown.
            process.kill()
            process.wait(timeout=5)
    assert db_conn.execute("SELECT status FROM agents_meta WHERE id=%s", (agent,)).fetchone() == (
        "idling",
    )


def test_checkpoint_replaced_by_real_second_connection_refuses_parking(
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_identity: MachineIdentity,
) -> None:
    from base.deploy.maintenance import cold

    agent = _retired(db_conn, machine_identity=machine_identity)
    original = cold.require_persisted_end

    def replace(conn: psycopg.Connection[Any], agent_id: int) -> str:
        checkpoint = original(conn, agent_id)
        with psycopg.connect(db_conn.info.dsn, autocommit=True) as writer:
            _persist_end(writer, agent_id)
        return checkpoint

    monkeypatch.setattr(cold, "require_persisted_end", replace)
    with pytest.raises(RuntimeError, match="checkpoint changed"):
        _prepare(db_conn, machine_identity=machine_identity)
    assert db_conn.execute("SELECT status FROM agents_meta WHERE id=%s", (agent,)).fetchone() == (
        "idling",
    )


async def test_resume_admits_a_successor_without_rewriting_legacy_history(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    *,
    machine_identity: MachineIdentity,
) -> None:
    agent = _retired(db_conn, machine_identity=machine_identity)
    history = db_conn.execute(
        "SELECT * FROM inbound_messages WHERE agent_id=%s ORDER BY id", (agent,)
    ).fetchall()
    checkpoint = PostgresSaver(db_conn).get_tuple({"configurable": {"thread_id": str(agent)}})
    db_conn.commit()
    _prepare(db_conn, machine_identity=machine_identity)
    current = pause_owner.read()
    assert current.maintenance is not None
    cohort.verify_drained(db_conn, current.maintenance)
    db_conn.commit()
    owner = uuid4()
    assert (
        await admit_hosted_runtime(
            aops_pool, agent, machine_identity.name(), owner, expected_from="idling", db=database
        )
        is None
    )
    resume_agents(database, event_bus)
    admitted = await admit_hosted_runtime(
        aops_pool, agent, machine_identity.name(), owner, expected_from="idling", db=database
    )
    assert admitted is not None and admitted.owner == owner
    assert await settle_hosted_runtime(
        aops_pool,
        admitted,
        bus=event_bus,
    )
    assert db_conn.execute(
        "SELECT status,runtime_owner FROM agents_meta WHERE id=%s", (agent,)
    ).fetchone() == ("idling", owner)
    assert (
        db_conn.execute(
            "SELECT * FROM inbound_messages WHERE agent_id=%s ORDER BY id", (agent,)
        ).fetchall()
        == history
    )
    assert (
        PostgresSaver(db_conn).get_tuple({"configurable": {"thread_id": str(agent)}}) == checkpoint
    )
