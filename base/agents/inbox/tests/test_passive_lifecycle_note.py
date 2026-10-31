from __future__ import annotations

import json

import psycopg
import pytest

from base.agents.inbox.passive_lifecycle_note import insert_passive_host_lifecycle_notes
from base.agents.messages.inbound import not_passive_inbound
from base.db import create_agent


def _spawn_with_meta(db_conn: psycopg.Connection, *, status: str, machine: str) -> int:
    agent_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta (id, spawner, status, machine) VALUES (%s, 'user', %s, %s)",
        (agent_id, status, machine),
    )
    return agent_id


def test_passive_host_lifecycle_notes_reach_only_eligible_local_live_agents(
    db_conn: psycopg.Connection,
) -> None:
    idle_local = _spawn_with_meta(db_conn, status="idling", machine="runner-a")
    running_owed = _spawn_with_meta(db_conn, status="running", machine="runner-a")
    running_unowed = _spawn_with_meta(db_conn, status="running", machine="runner-a")
    already_restarting = _spawn_with_meta(db_conn, status="idling", machine="runner-a")
    already_terminating = _spawn_with_meta(db_conn, status="idling", machine="runner-a")
    terminated_local = _spawn_with_meta(db_conn, status="terminated", machine="runner-a")
    idle_remote = _spawn_with_meta(db_conn, status="idling", machine="runner-b")

    db_conn.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source, status) "
        "VALUES (%s, '', 'restart', 'user', 'pending'), (%s, '', 'terminate', 'user', 'claimed')",
        (already_restarting, already_terminating),
    )

    written = insert_passive_host_lifecycle_notes(
        db_conn,
        machine="runner-a",
        source="system:restart",
        message="Zhiyuan updating cluster to fix lifecycle notes",
        exclude_agent_ids=[running_owed],
    )
    db_conn.commit()

    assert written == 2
    rows = db_conn.execute(
        "SELECT agent_id, content, kind, source, status, payload::text "
        "FROM inbound_messages WHERE kind = 'system_note' ORDER BY agent_id"
    ).fetchall()
    assert [r[0] for r in rows] == sorted([idle_local, running_unowed])
    for _aid, content, kind, source, status, payload_raw in rows:
        assert kind == "system_note"
        assert source == "system:restart"
        assert status == "pending"
        assert "Restart was accepted from system:restart" in content
        assert "Zhiyuan updating cluster to fix lifecycle notes" in content
        assert json.loads(payload_raw) == {"note_tag": "lifecycle_restart", "passive": True}

    # meiva:note
    # Passive notes are excluded by not_passive_inbound(), while normal notes remain visible.
    db_conn.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source, status, payload) "
        "VALUES (%s, 'active note', 'system_note', 'system', 'pending', '{\"note_tag\":\"context\"}'::jsonb)",
        (idle_local,),
    )
    active_rows = db_conn.execute(
        psycopg.sql.SQL(
            "SELECT content FROM inbound_messages m WHERE m.agent_id = %s AND {not_passive}"
        ).format(not_passive=psycopg.sql.SQL(not_passive_inbound("m"))),
        (idle_local,),
    ).fetchall()
    assert active_rows == [("active note",)]

    for excluded in (
        running_owed,
        already_restarting,
        already_terminating,
        terminated_local,
        idle_remote,
    ):
        count = db_conn.execute(
            "SELECT count(*) FROM inbound_messages WHERE agent_id = %s AND kind = 'system_note'",
            (excluded,),
        ).fetchone()
        assert count == (0,)


@pytest.mark.parametrize("blank", ["", "   "])
def test_passive_host_lifecycle_notes_refuse_blank_message(
    db_conn: psycopg.Connection, blank: str
) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        insert_passive_host_lifecycle_notes(
            db_conn, machine="runner-a", source="system:restart", message=blank
        )
