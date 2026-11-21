from __future__ import annotations

import json
import re
from pathlib import Path

import psycopg
import pytest

from base.agents.inbox.insert import insert_inbound_message_in_transaction
from base.agents.messages.kwargs import NoteTag
from base.db import create_agent

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SCHEMA_SQL = (_REPO_ROOT / "db" / "schema.sql").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("payload", "exc_type", "pattern"),
    [
        (None, ValueError, "requires a payload with 'note_tag'"),
        ({}, ValueError, "requires a payload with 'note_tag'"),
        ({"passive": True}, TypeError, "must be a NoteTag value"),
        ({"note_tag": 123}, TypeError, "must be a NoteTag value"),
        ({"note_tag": "not_a_real_tag"}, ValueError, "is not a NoteTag value"),
    ],
)
def test_insert_system_note_rejects_missing_or_invalid_note_tag(
    db_conn: psycopg.Connection,
    payload: dict[str, object] | None,
    exc_type: type[Exception],
    pattern: str,
) -> None:
    agent_id = create_agent(db_conn)
    with pytest.raises(exc_type, match=pattern), db_conn.cursor() as cur:
        insert_inbound_message_in_transaction(
            cur,
            agent_id,
            "notice",
            "system",
            kind="system_note",
            payload=payload,
        )
    db_conn.rollback()


def test_insert_system_note_accepts_valid_note_tag(db_conn: psycopg.Connection) -> None:
    agent_id = create_agent(db_conn)
    with db_conn.cursor() as cur:
        inbound_id, _ = insert_inbound_message_in_transaction(
            cur,
            agent_id,
            "cancelled schedules",
            "system:schedules",
            kind="system_note",
            payload={"note_tag": NoteTag.LIFECYCLE_TERMINATE.value, "passive": True},
        )
    db_conn.commit()
    row = db_conn.execute(
        "SELECT kind, payload FROM inbound_messages WHERE id = %s", (inbound_id,)
    ).fetchone()
    assert row == ("system_note", {"note_tag": "lifecycle_terminate", "passive": True})


@pytest.mark.parametrize(
    "raw_payload",
    [None, "{}", '{"passive": true}', '{"note_tag": ""}', '{"note_tag": "   "}', '{"note_tag": 7}'],
)
def test_db_check_rejects_raw_system_note_without_note_tag(
    db_conn: psycopg.Connection,
    raw_payload: str | None,
) -> None:
    agent_id = create_agent(db_conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        db_conn.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
            "VALUES (%s, 'raw poison pill', 'system_note', 'system:schedules', %s::jsonb)",
            (agent_id, raw_payload),
        )
    db_conn.rollback()


# meiva:note
# Every SQL trigger inserting a system_note inbound must set a legal NoteTag.
def test_every_schema_sql_system_note_insert_declares_a_valid_note_tag() -> None:
    inserts = re.findall(
        r"INSERT INTO\s+inbound_messages\b.*?;",
        _SCHEMA_SQL,
        re.DOTALL | re.IGNORECASE,
    )
    system_note_inserts = [stmt for stmt in inserts if "'system_note'" in stmt]
    assert system_note_inserts, "expected at least one SQL trigger system_note INSERT in schema.sql"

    valid_tags = {tag.value for tag in NoteTag}
    for stmt in system_note_inserts:
        json_literals = re.findall(r"'(\{[^']*\})'::jsonb", stmt)
        assert json_literals, (
            f"SQL system_note INSERT lacks an inline jsonb payload literal:\n{stmt}"
        )
        parsed = json.loads(json_literals[-1])
        tag = parsed.get("note_tag")
        assert isinstance(tag, str) and tag in valid_tags, (
            f"SQL system_note INSERT has missing or invalid note_tag {tag!r}:\n{stmt}"
        )


def test_cancel_schedules_trigger_uses_lifecycle_terminate_note_tag() -> None:
    match = re.search(
        r"CREATE OR REPLACE FUNCTION cancel_agent_schedules_on_termination\(\)\s*"
        r"RETURNS TRIGGER AS \$\$(.*?)\$\$ LANGUAGE plpgsql;",
        _SCHEMA_SQL,
        re.DOTALL,
    )
    assert match is not None, "cancel_agent_schedules_on_termination() not found in db/schema.sql"
    json_match = re.search(r"'(\{[^']*\})'::jsonb", match.group(1))
    assert json_match is not None
    payload = json.loads(json_match.group(1))
    assert payload == {"note_tag": NoteTag.LIFECYCLE_TERMINATE.value, "passive": True}
