import json
from types import SimpleNamespace
from typing import Annotated, Any, TypedDict, cast
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AnyMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import Checkpoint, empty_checkpoint
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool

from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from services.agent_runner.agent_host.health_routes import prepare_harness_switch_route
from services.agent_runner.agent_host.pooled_checkpoint import (
    PooledPostgresSaver,
    StaleIncarnationError,
)


def _config(thread: str = "9101", namespace: str = "", parent: str | None = None) -> RunnableConfig:
    configurable: dict[str, Any] = {"thread_id": thread, "checkpoint_ns": namespace}
    if parent is not None:
        configurable["checkpoint_id"] = parent
    return {"configurable": configurable}


def _checkpoint(version: str = "1") -> Checkpoint:
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"value": ["persisted"]}
    checkpoint["channel_versions"] = {"value": version}
    return checkpoint


def _saver(
    pool: AsyncConnectionPool[Any],
    *,
    expected_owner: UUID | None = None,
    machine: str | None = None,
) -> PooledPostgresSaver:
    return PooledPostgresSaver(
        cast(AsyncConnectionPool[AsyncConnection[DictRow]], pool),
        expected_owner=expected_owner,
        machine=machine,
    )


async def _setup_agent(
    pool: AsyncConnectionPool[Any],
    agent_id: int,
    *,
    status: str = "running",
    machine: str = "test-box",
    owner: UUID | None = None,
) -> None:
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO agents (id) VALUES (%s) ON CONFLICT DO NOTHING", (agent_id,)
        )
        await conn.execute(
            "INSERT INTO agents_meta (id, status, machine, runtime_owner) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET "
            "  status = EXCLUDED.status, "
            "  machine = EXCLUDED.machine, "
            "  runtime_owner = EXCLUDED.runtime_owner",
            (agent_id, status, machine, owner),
        )


async def test_matching_owner_writes_succeed(
    aops_pool: AsyncConnectionPool[Any],
    adb_conn: AsyncConnection,
) -> None:
    agent_id = 9101
    owner = uuid4()
    machine = "test-box"
    await _setup_agent(aops_pool, agent_id, status="running", machine=machine, owner=owner)

    saver = _saver(aops_pool, expected_owner=owner, machine=machine)
    config = _config(str(agent_id))

    saved_config = await saver.aput(
        config, _checkpoint("1"), {"source": "input", "step": -1}, {"value": "1"}
    )
    assert saved_config is not None

    await saver.aput_writes(saved_config, [("messages", "first reply")], "task-1")

    async with aops_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) FROM checkpoints WHERE thread_id = %s", (str(agent_id),)
        )
        assert (await cur.fetchone()) == (1,)

        cur = await conn.execute(
            "SELECT count(*) FROM checkpoint_writes WHERE thread_id = %s", (str(agent_id),)
        )
        assert (await cur.fetchone()) == (1,)


async def test_zombie_runner_takeover_rejection_during_aput_with_zero_stale_rows(
    aops_pool: AsyncConnectionPool[Any],
    adb_conn: AsyncConnection,
) -> None:
    agent_id = 9102
    old_owner = uuid4()
    new_owner = uuid4()
    machine = "test-box"

    await _setup_agent(aops_pool, agent_id, status="running", machine=machine, owner=old_owner)
    zombie_saver = _saver(aops_pool, expected_owner=old_owner, machine=machine)

    config = _config(str(agent_id))
    saved1 = await zombie_saver.aput(
        config, _checkpoint("1"), {"source": "input", "step": -1}, {"value": "1"}
    )

    async with aops_pool.connection() as conn, conn.transaction():
        await conn.execute(
            "UPDATE agents_meta SET runtime_owner = %s WHERE id = %s", (new_owner, agent_id)
        )

    cp2 = _checkpoint("2")
    saved_configurable = saved1.get("configurable", {})
    parent_id = str(saved_configurable["checkpoint_id"])
    config2 = _config(str(agent_id), parent=parent_id)
    with pytest.raises(StaleIncarnationError, match="stale runner write"):
        await zombie_saver.aput(config2, cp2, {"source": "loop", "step": 0}, {"value": "2"})

    async with aops_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) FROM checkpoints WHERE thread_id = %s", (str(agent_id),)
        )
        assert (await cur.fetchone()) == (1,)

        cur = await conn.execute(
            "SELECT count(*) FROM checkpoints WHERE thread_id = %s AND checkpoint_id = %s",
            (str(agent_id), cp2["id"]),
        )
        assert (await cur.fetchone()) == (0,)


async def test_zombie_runner_takeover_rejection_during_aput_writes_with_zero_stale_rows(
    aops_pool: AsyncConnectionPool[Any],
    adb_conn: AsyncConnection,
) -> None:
    agent_id = 9103
    old_owner = uuid4()
    new_owner = uuid4()
    machine = "test-box"

    await _setup_agent(aops_pool, agent_id, status="running", machine=machine, owner=old_owner)
    zombie_saver = _saver(aops_pool, expected_owner=old_owner, machine=machine)

    config = _config(str(agent_id))
    saved1 = await zombie_saver.aput(
        config, _checkpoint("1"), {"source": "input", "step": -1}, {"value": "1"}
    )

    async with aops_pool.connection() as conn, conn.transaction():
        await conn.execute(
            "UPDATE agents_meta SET runtime_owner = %s WHERE id = %s", (new_owner, agent_id)
        )

    with pytest.raises(StaleIncarnationError, match="stale runner write"):
        await zombie_saver.aput_writes(saved1, [("messages", "stale reply")], "task-zombie")

    async with aops_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) FROM checkpoint_writes WHERE thread_id = %s", (str(agent_id),)
        )
        assert (await cur.fetchone()) == (0,)


async def test_stale_runner_rejection_during_adelete_thread(
    aops_pool: AsyncConnectionPool[Any],
    adb_conn: AsyncConnection,
) -> None:
    agent_id = 9104
    old_owner = uuid4()
    new_owner = uuid4()
    machine = "test-box"

    await _setup_agent(aops_pool, agent_id, status="running", machine=machine, owner=old_owner)
    zombie_saver = _saver(aops_pool, expected_owner=old_owner, machine=machine)

    config = _config(str(agent_id))
    await zombie_saver.aput(
        config, _checkpoint("1"), {"source": "input", "step": -1}, {"value": "1"}
    )

    async with aops_pool.connection() as conn, conn.transaction():
        await conn.execute(
            "UPDATE agents_meta SET runtime_owner = %s WHERE id = %s", (new_owner, agent_id)
        )

    with pytest.raises(StaleIncarnationError, match="stale runner write"):
        await zombie_saver.adelete_thread(str(agent_id))

    async with aops_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) FROM checkpoints WHERE thread_id = %s", (str(agent_id),)
        )
        assert (await cur.fetchone()) == (1,)


async def test_active_daemon_writes_succeed_during_idling_after_turn(
    aops_pool: AsyncConnectionPool[Any],
    adb_conn: AsyncConnection,
) -> None:
    agent_id = 9105
    owner = uuid4()
    machine = "test-box"

    await _setup_agent(aops_pool, agent_id, status="idling", machine=machine, owner=owner)

    saver = _saver(aops_pool, expected_owner=owner, machine=machine)
    config = _config(str(agent_id))

    saved = await saver.aput(
        config, _checkpoint("1"), {"source": "input", "step": -1}, {"value": "1"}
    )
    assert saved is not None


async def test_active_daemon_writes_succeed_during_idling_before_first_turn(
    aops_pool: AsyncConnectionPool[Any],
    adb_conn: AsyncConnection,
) -> None:
    agent_id = 9106
    daemon_owner = uuid4()
    machine = "test-box"

    await _setup_agent(aops_pool, agent_id, status="idling", machine=machine, owner=None)

    saver = _saver(aops_pool, expected_owner=daemon_owner, machine=machine)
    config = _config(str(agent_id))

    saved = await saver.aput(
        config, _checkpoint("1"), {"source": "input", "step": -1}, {"value": "1"}
    )
    assert saved is not None


async def test_daemon_rejects_idle_agent_on_different_machine(
    aops_pool: AsyncConnectionPool[Any],
    adb_conn: AsyncConnection,
) -> None:
    agent_id = 9107
    daemon_owner = uuid4()
    machine_local = "test-box-local"
    machine_foreign = "test-box-foreign"

    await _setup_agent(aops_pool, agent_id, status="idling", machine=machine_foreign, owner=None)

    saver = _saver(aops_pool, expected_owner=daemon_owner, machine=machine_local)
    config = _config(str(agent_id))

    with pytest.raises(StaleIncarnationError, match="not expected machine"):
        await saver.aput(config, _checkpoint("1"), {"source": "input", "step": -1}, {"value": "1"})


async def test_terminated_agent_write_rejected_but_delete_allowed(
    aops_pool: AsyncConnectionPool[Any],
    adb_conn: AsyncConnection,
) -> None:
    agent_id = 9108
    owner = uuid4()
    machine = "test-box"

    await _setup_agent(aops_pool, agent_id, status="running", machine=machine, owner=owner)
    saver = _saver(aops_pool, expected_owner=owner, machine=machine)

    config = _config(str(agent_id))
    await saver.aput(config, _checkpoint("1"), {"source": "input", "step": -1}, {"value": "1"})

    async with aops_pool.connection() as conn, conn.transaction():
        await conn.execute(
            "UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,)
        )

    with pytest.raises(StaleIncarnationError, match="terminated agent"):
        await saver.aput(config, _checkpoint("2"), {"source": "loop", "step": 0}, {"value": "2"})

    await saver.adelete_thread(str(agent_id))

    async with aops_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) FROM checkpoints WHERE thread_id = %s", (str(agent_id),)
        )
        assert (await cur.fetchone()) == (0,)


async def test_unbound_control_plane_saver_passes_through(
    aops_pool: AsyncConnectionPool[Any],
    adb_conn: AsyncConnection,
) -> None:
    agent_id = 9109
    owner = uuid4()
    await _setup_agent(aops_pool, agent_id, status="running", owner=owner)

    saver = _saver(aops_pool, expected_owner=None)
    config = _config(str(agent_id))

    saved = await saver.aput(
        config, _checkpoint("1"), {"source": "input", "step": -1}, {"value": "1"}
    )
    assert saved is not None


async def test_non_agent_threads_pass_unfenced_cleanly(
    aops_pool: AsyncConnectionPool[Any],
    adb_conn: AsyncConnection,
) -> None:
    thread_id = "999999"
    cur = await adb_conn.execute("SELECT 1 FROM agents_meta WHERE id = %s", (int(thread_id),))
    assert (await cur.fetchone()) is None

    saver = _saver(aops_pool, expected_owner=uuid4(), machine="test-box")
    config = _config(thread_id)

    saved = await saver.aput(
        config, _checkpoint("1"), {"source": "input", "step": -1}, {"value": "1"}
    )
    assert saved is not None

    str_config = _config("non-numeric-subgraph")
    saved_str = await saver.aput(
        str_config, _checkpoint("1"), {"source": "input", "step": -1}, {"value": "1"}
    )
    assert saved_str is not None


async def test_saver_with_owner_rebind(
    aops_pool: AsyncConnectionPool[Any],
    adb_conn: AsyncConnection,
) -> None:
    agent_id = 9110
    owner = uuid4()
    machine = "test-box"
    await _setup_agent(aops_pool, agent_id, status="running", machine=machine, owner=owner)

    unbound_saver = _saver(aops_pool)
    bound_saver = unbound_saver.with_owner(owner, machine=machine)

    assert bound_saver.expected_owner == owner
    assert bound_saver.machine == machine

    config = _config(str(agent_id))
    saved = await bound_saver.aput(
        config, _checkpoint("1"), {"source": "input", "step": -1}, {"value": "1"}
    )
    assert saved is not None


class _SwitchState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


def _noop(state: _SwitchState) -> _SwitchState:
    return state


def _build_test_graph(saver: PooledPostgresSaver) -> Any:
    builder: Any = StateGraph(_SwitchState)
    builder.add_node("noop", _noop)
    builder.add_edge(START, "noop")
    builder.add_edge("noop", END)
    return builder.compile(checkpointer=saver)


def _drop_agent(_aid: int) -> None:
    pass


@pytest.mark.parametrize("before_first_turn", [True, False])
async def test_prepare_harness_switch_with_fenced_saver_succeeds(
    aops_pool: AsyncConnectionPool[Any],
    adb_conn: AsyncConnection,
    before_first_turn: bool,
) -> None:
    agent_id = 9111 if before_first_turn else 9112
    owner = uuid4()
    machine = "test-box"

    await _setup_agent(
        aops_pool,
        agent_id,
        status="idling",
        machine=machine,
        owner=None if before_first_turn else owner,
    )

    saver = _saver(aops_pool, expected_owner=owner, machine=machine)
    graph = _build_test_graph(saver)
    host = SimpleNamespace(_graph=graph, _machine=machine, drop_agent=_drop_agent)
    route = prepare_harness_switch_route(
        cast(Any, host), clock=Clock(ClockConfig("UTC", "UTC", False), timers=SYSTEM_TIMERS)
    )

    payload = json.dumps(
        {
            "agent_id": agent_id,
            "old_harness": "meiva",
            "new_harness": "claude_code",
            "source": "operator",
            "message": "testing switch under fencing",
        }
    ).encode("utf-8")

    status_code, body, _content_type = await route(payload)
    assert status_code == 200
    assert b'"ok":true' in body

    state = await graph.aget_state({"configurable": {"thread_id": str(agent_id)}})
    assert state.values.get("messages") is not None
    assert len(state.values["messages"]) >= 1
    assert "Harness switched from 'meiva' to 'claude_code'" in state.values["messages"][0].content
