from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import psycopg
import pytest
from langchain_core.messages import HumanMessage
from langgraph.graph import END
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from agent.db import claim_inbound_batch
from agent.graph import claim_node
from agent.graph.claim.tests.claim_support import make_runtime, thread_config
from agent.ownership.corpse_reap import reap_crash_corpses
from agent.ownership.hosted import admit_hosted_runtime, apply_hosted_lifecycle, stamp_turn_fatal
from agent.state import AgentState
from base.agents.context import MeivaContext
from base.agents.history.timeline import build_timeline_items
from base.agents.messages.kwargs import MeivaMsgType, NoteTag
from base.agents.messages.timestamp_policy import TimestampPolicy
from base.agents.tests.langgraph_views import command_update, update_messages
from base.cluster import inbound_channel
from base.config import settings
from base.db import PG_KEEPALIVE_KWARGS, Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.redis_client import open_sync_redis
from base.events.live.tests.rng import seeded_auth_retry
from base.native_process.runtime_incarnation import RuntimeIncarnation
from ops.lifecycle import terminate_force_blocking
from ops.lifecycle.termination import (
    enqueue_termination_inbounds,
    force_mark_terminated,
    force_terminate_with_notice,
)
from ops.rpc_schemas import TerminateAgentRequest
from services.agent_runner.agent_host.lifecycle.shells import (
    apply_lifecycle_command,
    reap_and_announce_corpses,
)


def _runtime_with_incarnation(
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
    incarnation: RuntimeIncarnation,
) -> Runtime[MeivaContext]:
    rt = make_runtime(ops_pool=aops_pool, database_gate=database_gate)
    return Runtime(context=replace(rt.context, original_incarnation=incarnation))


def _make_agent(
    conn: psycopg.Connection[Any],
    *,
    spawner: str = "user",
    born_spawner: str | None = None,
    status: str = "idling",
    label: str | None = None,
    machine: str = "notify-test-host",
) -> int:
    agent_id = create_agent(conn)
    if label is not None:
        conn.execute("UPDATE agents SET label = %s WHERE id = %s", (label, agent_id))
    effective_born = spawner if born_spawner is None else born_spawner
    conn.execute(
        "INSERT INTO agents_meta (id, spawner, born_spawner, status, machine) "
        "VALUES (%s, %s, %s, %s, %s)",
        (agent_id, spawner, effective_born, status, machine),
    )
    conn.commit()
    return agent_id


def _ancestor_notices(
    conn: psycopg.Connection[Any], ancestor_id: int
) -> list[tuple[int, str, str, str, dict[str, object]]]:
    rows = conn.execute(
        "SELECT id, kind, source, content, payload FROM inbound_messages "
        "WHERE agent_id = %s AND kind = 'system_note' "
        "AND payload ->> 'note_tag' = %s ORDER BY id",
        (ancestor_id, NoteTag.AGENT_TERMINATED.value),
    ).fetchall()
    return [(r[0], r[1], r[2], r[3], r[4]) for r in rows]


def _assert_single_marker(cmd: Any, expected_fragment: str) -> None:
    assert cmd.goto == END
    msgs = update_messages(command_update(cmd))
    assert len(msgs) == 1
    marker = msgs[0]
    assert isinstance(marker, HumanMessage)
    assert isinstance(marker.content, str)
    assert expected_fragment in marker.content
    assert marker.additional_kwargs.get("meiva_note_tag") == NoteTag.LIFECYCLE_TERMINATE.value


@pytest.fixture
def sync_pool(db_url: str) -> Iterator[ConnectionPool]:
    with ConnectionPool(
        settings.data_plane.db_url,
        min_size=1,
        max_size=2,
        kwargs={**PG_KEEPALIVE_KWARGS},
    ) as pool:
        yield cast(ConnectionPool, pool)


async def test_self_terminate_with_message_records_self_marker_and_notifies_parent_with_wake(
    redis_url: str,
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
    database_gate: ProcessDbGate,
    tmp_path: Path,
) -> None:
    parent_id = _make_agent(db_conn, spawner="user", status="idling", label="coordinator")
    child_id = _make_agent(
        db_conn, spawner=f"agent:{parent_id}", status="idling", label="step1-worker"
    )
    incarnation = await admit_hosted_runtime(
        aops_pool, child_id, "notify-test-host", uuid4(), db=database
    )
    assert incarnation is not None
    db_conn.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source) "
        "VALUES (%s, %s, 'terminate', 'self')",
        (child_id, "Draft PR #9157 ready"),
    )
    db_conn.commit()

    cmd = await claim_node(
        AgentState(),
        _runtime_with_incarnation(aops_pool, database_gate, incarnation),
        thread_config(child_id),
    )
    _assert_single_marker(cmd, "Termination was accepted from yourself: Draft PR #9157 ready")

    listener = open_sync_redis(redis_url, decode_responses=True, auth_retry=seeded_auth_retry())
    pubsub = cast(Any, listener).pubsub()
    pubsub.subscribe(inbound_channel(parent_id, event_bus.channel))
    assert pubsub.get_message(timeout=5)["type"] == "subscribe"

    try:
        kind = await apply_lifecycle_command(
            aops_pool,
            incarnation,
            bus=event_bus,
            resume_turn=False,
            home=tmp_path,
        )
        assert kind == "terminate"
        woken = pubsub.get_message(timeout=5)
    finally:
        pubsub.close()
        listener.close()

    notices = _ancestor_notices(db_conn, parent_id)
    assert len(notices) == 1
    inbound_id, row_kind, row_source, content, payload = notices[0]
    assert (row_kind, row_source) == ("system_note", "system")
    assert payload == {
        "note_tag": "agent_terminated",
        "terminated_agent_id": child_id,
        "termination_source": "self",
    }
    assert f"Agent #{child_id} (step1-worker) terminated (by self)." in content
    assert "Draft PR #9157 ready" in content
    assert woken is not None
    assert woken["data"] == str(inbound_id)

    audit = db_conn.execute(
        "SELECT source, attributes FROM audit_events "
        "WHERE agent_id = %s AND event_name = 'terminate' ORDER BY id",
        (child_id,),
    ).fetchall()
    assert len(audit) == 1
    assert audit[0][0] == "self"
    assert audit[0][1]["message"] == "Draft PR #9157 ready"


def test_agent_terminated_system_note_renders_on_timeline_with_timestamp() -> None:
    items, _ = build_timeline_items(
        [
            HumanMessage(
                content="Agent #8528 (worker) terminated (by self).\n\nMessage:\nDone",
                additional_kwargs={
                    "meiva_msg_type": MeivaMsgType.SYSTEM_NOTE.value,
                    "meiva_note_tag": NoteTag.AGENT_TERMINATED.value,
                    "meiva_created_at": "2026-11-22T15:00:00+00:00",
                },
            )
        ],
        inputs=TimestampPolicy.off(),
    )
    assert len(items) == 1
    assert items[0].kind == "system_marker"
    assert items[0].source == NoteTag.AGENT_TERMINATED.value
    assert items[0].show_timestamp is True


async def test_dead_parent_is_skipped_and_living_grandparent_is_notified(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool,
    database: Database,
) -> None:
    grandparent_id = _make_agent(db_conn, spawner="user", status="running", label="ceo")
    dead_parent_id = _make_agent(
        db_conn, spawner=f"agent:{grandparent_id}", status="terminated", label="dead-lead"
    )
    child_id = _make_agent(
        db_conn, spawner=f"agent:{dead_parent_id}", status="idling", label="leaf-worker"
    )
    incarnation = await admit_hosted_runtime(
        aops_pool, child_id, "notify-test-host", uuid4(), db=database
    )
    assert incarnation is not None
    db_conn.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source, status) "
        "VALUES (%s, %s, 'terminate', 'self', 'claimed')",
        (child_id, "Finished leaf job"),
    )
    db_conn.commit()

    applied = await apply_hosted_lifecycle(aops_pool, incarnation, resume_turn=False)
    assert applied is not None
    assert applied.kind == "terminate"
    assert applied.ancestor_notification is not None
    assert applied.ancestor_notification.ancestor_id == grandparent_id
    assert _ancestor_notices(db_conn, dead_parent_id) == []

    notices = _ancestor_notices(db_conn, grandparent_id)
    assert len(notices) == 1
    assert f"Agent #{child_id} (leaf-worker) terminated (by self)." in notices[0][3]
    assert "Finished leaf job" in notices[0][3]


async def test_third_party_graceful_terminate_records_child_marker_and_notifies_parent(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool,
    sync_pool: ConnectionPool,
    database: Database,
    event_bus: EventBus,
    database_gate: ProcessDbGate,
) -> None:
    parent_id = _make_agent(db_conn, spawner="user", status="idling", label="lead")
    third_party_id = _make_agent(db_conn, spawner="user", status="idling", label="auditor")
    child_id = _make_agent(db_conn, spawner=f"agent:{parent_id}", status="idling", label="worker-a")
    inc = await admit_hosted_runtime(aops_pool, child_id, "notify-test-host", uuid4(), db=database)
    assert inc is not None

    enqueue_termination_inbounds(
        database,
        event_bus,
        child_id,
        sync_pool,
        source=f"agent:{third_party_id}",
        message="Superseded by plan B",
    )
    cmd = await claim_node(
        AgentState(),
        _runtime_with_incarnation(aops_pool, database_gate, inc),
        thread_config(child_id),
    )
    _assert_single_marker(
        cmd, f"Termination was accepted from agent:{third_party_id}: Superseded by plan B"
    )
    applied = await apply_hosted_lifecycle(aops_pool, inc, resume_turn=False)
    assert applied is not None
    assert applied.ancestor_notification is not None
    assert applied.ancestor_notification.ancestor_id == parent_id

    notices = _ancestor_notices(db_conn, parent_id)
    assert len(notices) == 1
    assert f"Agent #{child_id} (worker-a) terminated (by agent #{third_party_id})." in notices[0][3]
    assert "Superseded by plan B" in notices[0][3]


def test_user_force_terminate_retains_child_chat_and_notifies_parent(
    db_conn: psycopg.Connection[Any],
    sync_pool: ConnectionPool,
    database: Database,
    event_bus: EventBus,
) -> None:
    parent_id = _make_agent(db_conn, spawner="user", status="idling", label="lead")
    child_id = _make_agent(
        db_conn, spawner=f"agent:{parent_id}", status="running", label="worker-b"
    )

    terminate_force_blocking(
        database,
        event_bus,
        child_id,
        TerminateAgentRequest(force=True, source="user", message="Stopped by operator"),
        sync_pool,
    )

    child_chats = db_conn.execute(
        "SELECT content, status FROM inbound_messages WHERE agent_id = %s AND kind = 'chat'",
        (child_id,),
    ).fetchall()
    assert child_chats == [("Stopped by operator", "pending")]
    notices = _ancestor_notices(db_conn, parent_id)
    assert len(notices) == 1
    assert f"Agent #{child_id} (worker-b) terminated (by user)." in notices[0][3]
    assert "Stopped by operator" in notices[0][3]


async def test_corpse_reaper_forwards_fatal_reason_to_nearest_living_ancestor(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
    tmp_path: Path,
) -> None:
    parent_id = _make_agent(db_conn, spawner="user", status="idling", label="lead")
    child_id = _make_agent(db_conn, spawner=f"agent:{parent_id}", status="idling", label="worker-c")
    inc = await admit_hosted_runtime(aops_pool, child_id, "notify-test-host", uuid4(), db=database)
    assert inc is not None
    assert (
        await stamp_turn_fatal(aops_pool, inc, reason="RuntimeError: upstream 503 fatal")
    ).applied
    db_conn.execute(
        "UPDATE agents_meta SET status = 'idling', "
        "last_turn_fatal_at = now() - interval '20 minutes' WHERE id = %s",
        (child_id,),
    )
    db_conn.commit()

    reaped = await reap_and_announce_corpses(
        aops_pool, "notify-test-host", bus=event_bus, home=tmp_path
    )
    assert [c.agent_id for c in reaped] == [child_id]
    assert reaped[0].ancestor_notification is not None
    assert reaped[0].ancestor_notification.ancestor_id == parent_id

    notices = _ancestor_notices(db_conn, parent_id)
    assert len(notices) == 1
    assert f"Agent #{child_id} (worker-c) terminated (by corpse_reaper)." in notices[0][3]
    assert "RuntimeError: upstream 503 fatal" in notices[0][3]


async def test_anti_echo_skips_when_actor_is_nearest_living_ancestor_only(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool,
    sync_pool: ConnectionPool,
    database: Database,
    event_bus: EventBus,
) -> None:
    grandparent_id = _make_agent(db_conn, spawner="user", status="idling", label="gp")
    parent_id = _make_agent(
        db_conn, spawner=f"agent:{grandparent_id}", status="idling", label="parent"
    )

    child_graceful = _make_agent(
        db_conn, spawner=f"agent:{parent_id}", status="idling", label="child-graceful"
    )
    inc_g = await admit_hosted_runtime(
        aops_pool, child_graceful, "notify-test-host", uuid4(), db=database
    )
    assert inc_g is not None
    enqueue_termination_inbounds(
        database,
        event_bus,
        child_graceful,
        sync_pool,
        source=f"agent:{parent_id}",
        message="Cleaning up worker",
    )
    await claim_inbound_batch(aops_pool, child_graceful, incarnation=inc_g)
    applied = await apply_hosted_lifecycle(aops_pool, inc_g, resume_turn=False)
    assert applied is not None
    assert applied.ancestor_notification is None
    assert _ancestor_notices(db_conn, parent_id) == []

    child_force = _make_agent(
        db_conn, spawner=f"agent:{parent_id}", status="running", label="child-force"
    )
    _, _, _, notice = await asyncio.to_thread(
        force_terminate_with_notice,
        child_force,
        sync_pool,
        source=f"agent:{parent_id}",
        message="Force cleanup",
    )
    assert notice is None
    assert _ancestor_notices(db_conn, parent_id) == []

    child_by_gp = _make_agent(
        db_conn, spawner=f"agent:{parent_id}", status="running", label="child-by-gp"
    )
    _, _, _, notice_gp = await asyncio.to_thread(
        force_terminate_with_notice,
        child_by_gp,
        sync_pool,
        source=f"agent:{grandparent_id}",
        message="Grandparent override",
    )
    assert notice_gp is not None
    assert notice_gp.ancestor_id == parent_id
    assert len(_ancestor_notices(db_conn, parent_id)) == 1
    assert f"by agent #{grandparent_id}" in _ancestor_notices(db_conn, parent_id)[0][3]

    dead_parent = _make_agent(
        db_conn, spawner=f"agent:{grandparent_id}", status="terminated", label="dead-parent"
    )
    grandchild = _make_agent(
        db_conn, spawner=f"agent:{dead_parent}", status="running", label="grandchild"
    )
    _, _, _, notice_dead = await asyncio.to_thread(
        force_terminate_with_notice,
        grandchild,
        sync_pool,
        source=f"agent:{grandparent_id}",
    )
    assert notice_dead is None
    assert _ancestor_notices(db_conn, grandparent_id) == []


async def test_root_agent_all_dead_ancestors_and_cycles_skip_safely(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool,
    sync_pool: ConnectionPool,
    database: Database,
) -> None:
    root_id = _make_agent(db_conn, spawner="user", status="idling", label="root")
    inc_root = await admit_hosted_runtime(
        aops_pool, root_id, "notify-test-host", uuid4(), db=database
    )
    assert inc_root is not None
    db_conn.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source, status) "
        "VALUES (%s, 'bye', 'terminate', 'self', 'claimed')",
        (root_id,),
    )
    db_conn.commit()
    applied_root = await apply_hosted_lifecycle(aops_pool, inc_root, resume_turn=False)
    assert applied_root is not None
    assert applied_root.ancestor_notification is None

    dead_root = _make_agent(db_conn, spawner="user", status="terminated")
    orphan = _make_agent(db_conn, spawner=f"agent:{dead_root}", status="running")
    _, _, _, orphan_notice = await asyncio.to_thread(
        force_terminate_with_notice, orphan, sync_pool, source="user"
    )
    assert orphan_notice is None
    assert _ancestor_notices(db_conn, dead_root) == []

    cyc_c = create_agent(db_conn)
    cyc_d = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta (id, spawner, born_spawner, status, machine) VALUES "
        "(%s, %s, NULL, 'terminated', 'notify-test-host'), "
        "(%s, %s, NULL, 'running', 'notify-test-host')",
        (cyc_c, f"agent:{cyc_d}", cyc_d, f"agent:{cyc_c}"),
    )
    db_conn.commit()
    _, _, _, cyc_notice = await asyncio.to_thread(
        force_terminate_with_notice, cyc_d, sync_pool, source="user"
    )
    assert cyc_notice is None


async def test_idempotent_repeated_terminate_and_harness_switch_do_not_duplicate_or_emit(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool,
    sync_pool: ConnectionPool,
    database: Database,
    event_bus: EventBus,
) -> None:
    parent_id = _make_agent(db_conn, spawner="user", status="idling", label="parent")
    child_id = _make_agent(db_conn, spawner=f"agent:{parent_id}", status="running", label="worker")

    _, _, _, switch_notice = await asyncio.to_thread(
        force_terminate_with_notice,
        child_id,
        sync_pool,
        source="user",
        preserve_shells=True,
    )
    assert switch_notice is None
    assert _ancestor_notices(db_conn, parent_id) == []

    db_conn.execute(
        "UPDATE agents_meta SET status = 'running', termination_source = NULL WHERE id = %s",
        (child_id,),
    )
    db_conn.commit()

    force_mark_terminated(database, event_bus, child_id, sync_pool, source="user", message="once")
    assert len(_ancestor_notices(db_conn, parent_id)) == 1

    force_mark_terminated(database, event_bus, child_id, sync_pool, source="user", message="twice")
    assert len(_ancestor_notices(db_conn, parent_id)) == 1

    tid, _cutoff = enqueue_termination_inbounds(
        database, event_bus, child_id, sync_pool, source="user", message="thrice"
    )
    assert tid is None
    assert len(_ancestor_notices(db_conn, parent_id)) == 1

    assert await reap_crash_corpses(aops_pool, "notify-test-host") == []
    assert len(_ancestor_notices(db_conn, parent_id)) == 1
