from typing import Any, cast
from uuid import uuid4

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import Checkpoint, empty_checkpoint
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool

from base.native_process.runtime_incarnation import (
    RuntimeIncarnation,
    StaleIncarnationError,
)
from services.agent_runner.agent_host.pooled_checkpoint import (
    PooledPostgresSaver,
)


def _saver(
    pool: AsyncConnectionPool[Any],
    *,
    incarnation: RuntimeIncarnation | None = None,
) -> PooledPostgresSaver:
    return PooledPostgresSaver(
        cast(AsyncConnectionPool[AsyncConnection[DictRow]], pool),
        incarnation=incarnation,
    )


def _config(
    thread: str,
    namespace: str = "",
    parent: str | None = None,
    generation: object | None = None,
) -> RunnableConfig:
    configurable: dict[str, object] = {"thread_id": thread, "checkpoint_ns": namespace}
    if parent is not None:
        configurable["checkpoint_id"] = parent
    if generation is not None:
        configurable["runtime_generation"] = generation
    return cast(RunnableConfig, {"configurable": configurable})


def _checkpoint(version: str = "1") -> Checkpoint:
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"value": ["committed"]}
    checkpoint["channel_versions"] = {"value": version}
    return checkpoint


async def _insert_agent_meta(
    conn: AsyncConnection,
    agent_id: int,
    *,
    status: str = "running",
    generation: object | None = None,
    owner: object | None = None,
) -> None:
    await conn.execute("INSERT INTO agents (id) VALUES (%s) ON CONFLICT DO NOTHING", (agent_id,))
    await conn.execute(
        "INSERT INTO agents_meta (id, machine, status, runtime_generation, runtime_owner) "
        "VALUES (%s, 'test-mach', %s, %s, %s) "
        "ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, "
        "runtime_generation = EXCLUDED.runtime_generation, "
        "runtime_owner = EXCLUDED.runtime_owner",
        (agent_id, status, generation, owner),
    )
    await conn.commit()


async def test_active_generation_saves_checkpoints_and_writes_normally(
    aops_pool: AsyncConnectionPool,
    adb_conn: AsyncConnection,
) -> None:
    agent_id = 9101
    generation = uuid4()
    owner = uuid4()
    await _insert_agent_meta(
        adb_conn, agent_id, status="running", generation=generation, owner=owner
    )

    saver = _saver(aops_pool)
    config = _config(str(agent_id), generation=generation)
    cp1 = _checkpoint("1")
    parent_saved = await saver.aput(config, cp1, {"source": "input", "step": -1}, {"value": "1"})

    await saver.aput_writes(parent_saved, [("messages", "intermediate reply")], "task-1")

    cp2 = _checkpoint("2")
    child_saved = await saver.aput(parent_saved, cp2, {"source": "loop", "step": 0}, {"value": "2"})

    stored = await saver.aget_tuple(child_saved)
    assert stored is not None
    assert stored.checkpoint["id"] == cp2["id"]
    assert stored.parent_config is not None
    parent_cfg = stored.parent_config.get("configurable", {})
    saved_cfg = parent_saved.get("configurable", {})
    assert parent_cfg.get("checkpoint_id") == saved_cfg.get("checkpoint_id")

    cur = await adb_conn.execute(
        "SELECT count(*) FROM checkpoints WHERE thread_id = %s", (str(agent_id),)
    )
    assert (await cur.fetchone()) == (2,)

    cur = await adb_conn.execute(
        "SELECT count(*) FROM checkpoint_writes WHERE thread_id = %s", (str(agent_id),)
    )
    assert (await cur.fetchone()) == (1,)


async def test_takeover_generation_rotation_blocks_zombie_runner_aput_zero_rows(
    aops_pool: AsyncConnectionPool,
    adb_conn: AsyncConnection,
) -> None:
    agent_id = 9102
    generation_a = uuid4()
    owner_a = uuid4()
    await _insert_agent_meta(
        adb_conn, agent_id, status="running", generation=generation_a, owner=owner_a
    )

    saver = _saver(aops_pool)
    config_a = _config(str(agent_id), generation=generation_a)
    cp1 = _checkpoint("1")
    saved_parent = await saver.aput(config_a, cp1, {"source": "input", "step": -1}, {"value": "1"})

    cur = await adb_conn.execute(
        "SELECT count(*) FROM checkpoints WHERE thread_id = %s", (str(agent_id),)
    )
    assert (await cur.fetchone()) == (1,)

    # meiva:note
    # Runner B takes over admission and rotates runtime_generation in agents_meta.
    generation_b = uuid4()
    owner_b = uuid4()
    await adb_conn.execute(
        "UPDATE agents_meta SET runtime_generation = %s, runtime_owner = %s WHERE id = %s",
        (generation_b, owner_b, agent_id),
    )
    await adb_conn.commit()

    # meiva:note
    # Zombie Runner A attempts aput with stale generation: must fail closed with zero stale rows.
    zombie_cp = _checkpoint("2")
    with pytest.raises(StaleIncarnationError, match="stale checkpoint write"):
        await saver.aput(saved_parent, zombie_cp, {"source": "loop", "step": 0}, {"value": "2"})

    cur = await adb_conn.execute(
        "SELECT count(*) FROM checkpoints WHERE thread_id = %s", (str(agent_id),)
    )
    assert (await cur.fetchone()) == (1,)

    cur = await adb_conn.execute(
        "SELECT count(*) FROM checkpoint_blobs WHERE thread_id = %s AND version = '2'",
        (str(agent_id),),
    )
    assert (await cur.fetchone()) == (0,)

    # meiva:note
    # Legitimate Runner B saves child checkpoint with current generation cleanly.
    config_b = cast(
        RunnableConfig,
        {
            **saved_parent,
            "configurable": {
                **saved_parent.get("configurable", {}),
                "runtime_generation": generation_b,
            },
        },
    )
    legit_child = _checkpoint("2")
    saved_b = await saver.aput(config_b, legit_child, {"source": "loop", "step": 0}, {"value": "2"})
    assert saved_b is not None

    cur = await adb_conn.execute(
        "SELECT count(*) FROM checkpoints WHERE thread_id = %s", (str(agent_id),)
    )
    assert (await cur.fetchone()) == (2,)


async def test_takeover_generation_rotation_blocks_zombie_runner_aput_writes_zero_rows(
    aops_pool: AsyncConnectionPool,
    adb_conn: AsyncConnection,
) -> None:
    agent_id = 9103
    generation_a = uuid4()
    owner_a = uuid4()
    await _insert_agent_meta(
        adb_conn, agent_id, status="running", generation=generation_a, owner=owner_a
    )

    saver = _saver(aops_pool)
    config_a = _config(str(agent_id), generation=generation_a)
    cp1 = _checkpoint("1")
    saved_parent = await saver.aput(config_a, cp1, {"source": "input", "step": -1}, {"value": "1"})

    # meiva:note
    # Runner B takes over and rotates generation in agents_meta.
    generation_b = uuid4()
    await adb_conn.execute(
        "UPDATE agents_meta SET runtime_generation = %s WHERE id = %s",
        (generation_b, agent_id),
    )
    await adb_conn.commit()

    # meiva:note
    # Zombie Runner A attempts aput_writes with stale generation: must fail closed with 0 rows.
    with pytest.raises(StaleIncarnationError, match="stale checkpoint write"):
        await saver.aput_writes(saved_parent, [("messages", "stale zombie message")], "task-zombie")

    cur = await adb_conn.execute(
        "SELECT count(*) FROM checkpoint_writes WHERE thread_id = %s", (str(agent_id),)
    )
    assert (await cur.fetchone()) == (0,)

    # meiva:note
    # Legitimate Runner B saves writes cleanly with current generation.
    config_b = cast(
        RunnableConfig,
        {
            **saved_parent,
            "configurable": {
                **saved_parent.get("configurable", {}),
                "runtime_generation": generation_b,
            },
        },
    )
    await saver.aput_writes(config_b, [("messages", "valid runner b")], "task-b")

    cur = await adb_conn.execute(
        "SELECT count(*) FROM checkpoint_writes WHERE thread_id = %s", (str(agent_id),)
    )
    assert (await cur.fetchone()) == (1,)


async def test_unfenced_write_on_active_running_agent_fails_closed(
    aops_pool: AsyncConnectionPool,
    adb_conn: AsyncConnection,
) -> None:
    agent_id = 9104
    generation = uuid4()
    await _insert_agent_meta(
        adb_conn, agent_id, status="running", generation=generation, owner=uuid4()
    )

    saver = _saver(aops_pool)
    unfenced_config = _config(str(agent_id), generation=None)

    with pytest.raises(StaleIncarnationError, match="requires runtime_generation fencing token"):
        await saver.aput(
            unfenced_config, _checkpoint("1"), {"source": "input", "step": -1}, {"value": "1"}
        )

    with pytest.raises(StaleIncarnationError, match="requires runtime_generation fencing token"):
        await saver.aput_writes(unfenced_config, [("messages", "unfenced")], "task-unfenced")

    cur = await adb_conn.execute(
        "SELECT count(*) FROM checkpoints WHERE thread_id = %s", (str(agent_id),)
    )
    assert (await cur.fetchone()) == (0,)


async def test_terminated_agent_checkpoint_write_blocked(
    aops_pool: AsyncConnectionPool,
    adb_conn: AsyncConnection,
) -> None:
    agent_id = 9105
    generation = uuid4()
    await _insert_agent_meta(
        adb_conn, agent_id, status="terminated", generation=generation, owner=uuid4()
    )

    saver = _saver(aops_pool)
    config = _config(str(agent_id), generation=generation)

    with pytest.raises(StaleIncarnationError, match="stale checkpoint write"):
        await saver.aput(config, _checkpoint("1"), {"source": "input", "step": -1}, {"value": "1"})


async def test_saver_with_incarnation_binding(
    aops_pool: AsyncConnectionPool,
    adb_conn: AsyncConnection,
) -> None:
    agent_id = 9106
    generation = uuid4()
    owner = uuid4()
    incarnation = RuntimeIncarnation(agent_id, generation, owner)
    await _insert_agent_meta(
        adb_conn, agent_id, status="running", generation=generation, owner=owner
    )

    base_saver = _saver(aops_pool)
    bound_saver = base_saver.with_incarnation(incarnation)

    bare_config = _config(str(agent_id), generation=None)
    saved = await bound_saver.aput(
        bare_config, _checkpoint("1"), {"source": "input", "step": -1}, {"value": "1"}
    )
    assert saved is not None

    await bound_saver.aput_writes(saved, [("messages", "bound reply")], "task-bound")

    cp2 = _checkpoint("2")
    saved2 = await bound_saver.aput(saved, cp2, {"source": "loop", "step": 0}, {"value": "2"})
    assert saved2 is not None

    cur = await adb_conn.execute(
        "SELECT count(*) FROM checkpoints WHERE thread_id = %s", (str(agent_id),)
    )
    assert (await cur.fetchone()) == (2,)

    cur = await adb_conn.execute(
        "SELECT count(*) FROM checkpoint_writes WHERE thread_id = %s", (str(agent_id),)
    )
    assert (await cur.fetchone()) == (1,)


async def test_non_agent_threads_pass_unfenced_cleanly(
    aops_pool: AsyncConnectionPool,
    adb_conn: AsyncConnection,
) -> None:
    thread_id = "999999"
    cur = await adb_conn.execute("SELECT 1 FROM agents_meta WHERE id = %s", (int(thread_id),))
    assert (await cur.fetchone()) is None

    saver = _saver(aops_pool)
    config = _config(thread_id, generation=None)

    saved = await saver.aput(
        config, _checkpoint("1"), {"source": "input", "step": -1}, {"value": "1"}
    )
    assert saved is not None

    str_config = _config("non-numeric-thread", generation=None)
    saved_str = await saver.aput(
        str_config, _checkpoint("1"), {"source": "input", "step": -1}, {"value": "1"}
    )
    assert saved_str is not None
