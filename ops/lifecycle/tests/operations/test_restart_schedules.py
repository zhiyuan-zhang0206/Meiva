from pathlib import Path

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.db import Database
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from ops import lifecycle
from ops.lifecycle.tests.loopback.agent_host import ServedAgentHost
from ops.rpc_schemas import RestartAgentRequest


def _schedule(
    conn: psycopg.Connection, owner: int, policy: str = "cancel", suffix: str = ""
) -> int:
    row = conn.execute(
        "INSERT INTO schedules (name, script, command, machine, owner_agent_id, "
        "trigger_kind, interval_seconds, enabled, status, next_launch_at, on_owner_terminate) "
        "SELECT %s, 'pass', 'python run.py', machine, id, 'every', 60, true, "
        "'scheduled', now() + interval '1 hour', %s FROM agents_meta WHERE id=%s RETURNING id",
        (f"owned-{policy}{suffix}", policy, owner),
    ).fetchone()
    assert row is not None
    conn.commit()
    return row[0]


def _state(conn: psycopg.Connection, sid: int) -> tuple[object, ...]:
    row = conn.execute(
        "SELECT enabled, status, next_launch_at FROM schedules WHERE id=%s", (sid,)
    ).fetchone()
    conn.commit()
    assert row is not None
    return row


@pytest.mark.parametrize("overlay", [None, {"llm_model": "gpt-6.1-sol"}])
async def test_external_restart_preserves_schedules(
    db_conn: psycopg.Connection,
    db_pool: ConnectionPool,
    lifecycle_agent_id: int,
    database: Database,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    agent_host: ServedAgentHost,
    unit_home: Path,
    overlay: dict[str, object] | None,
) -> None:
    owner = lifecycle_agent_id
    db_conn.execute("UPDATE agents_meta SET harness='codex' WHERE id=%s", (owner,))
    db_conn.commit()
    sid = _schedule(db_conn, owner)
    before = _state(db_conn, sid)

    await lifecycle.restart_agent_op(
        database,
        event_bus,
        owner,
        RestartAgentRequest(source="user", message="restart", config_overlay=overlay),
        db_pool,
        host=agent_host.host,
        catalog=model_catalog,
        default_model="deepseek-flash",
        home=unit_home,
    )

    assert _state(db_conn, sid) == before
    assert db_conn.execute(
        "SELECT kind, status FROM inbound_messages WHERE agent_id=%s "
        "AND kind IN ('restart', 'terminate') ORDER BY id",
        (owner,),
    ).fetchall() == [("restart", "done")]
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND source='system:schedules'",
        (owner,),
    ).fetchone() == (0,)
    if overlay is not None:
        assert db_conn.execute(
            "SELECT config_overlay->>'llm_model' FROM agents_meta WHERE id=%s", (owner,)
        ).fetchone() == (overlay["llm_model"],)


@pytest.mark.parametrize("source", ["force", "user", "reaper"])
def test_real_death_after_an_old_restart_cancels_and_leaves_owner_notice(
    db_conn: psycopg.Connection,
    db_pool: ConnectionPool,
    lifecycle_agent_id: int,
    source: str,
) -> None:
    owner = lifecycle_agent_id
    cancelled = [_schedule(db_conn, owner), _schedule(db_conn, owner, suffix="-second")]
    kept = _schedule(db_conn, owner, "keep")
    before = _state(db_conn, kept)
    lifecycle.force_terminate_transaction(owner, db_pool, source="user", preserve_shells=True)
    assert all(_state(db_conn, sid)[0:2] == (True, "scheduled") for sid in cancelled)
    db_conn.execute(
        "UPDATE agents_meta SET status='idling', termination_source=NULL WHERE id=%s", (owner,)
    )
    db_conn.commit()
    if source == "force":
        lifecycle.force_terminate_transaction(owner, db_pool, source="user")
    else:
        db_conn.execute(
            "UPDATE agents_meta SET status='terminated', termination_source=%s WHERE id=%s",
            (source, owner),
        )
        db_conn.commit()
    assert all(_state(db_conn, sid) == (False, "cancelled", None) for sid in cancelled)
    assert _state(db_conn, kept) == before
    notes = db_conn.execute(
        "SELECT agent_id, content, kind, status, payload FROM inbound_messages "
        "WHERE source='system:schedules' ORDER BY id"
    ).fetchall()
    assert len(notes) == 1
    assert notes[0][0] == owner
    assert notes[0][2:] == ("system_note", "pending", {"passive": True})
    for sid in cancelled:
        assert f"- #{sid}: owned-cancel" in notes[0][1]
    assert f"#{kept}:" not in notes[0][1]
    # meiva:note
    # Resurrection leaves the passive notice available without recancelling.
    db_conn.execute(
        "UPDATE agents_meta SET status='idling', termination_source=NULL WHERE id=%s", (owner,)
    )
    db_conn.commit()
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE agent_id=%s AND source='system:schedules'",
        (owner,),
    ).fetchall() == [("pending",)]
