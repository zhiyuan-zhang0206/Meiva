import psycopg
from psycopg.types.json import Jsonb

from base.agents.questions.open_questions import read_open_questions
from base.db import create_agent


def _ask(conn: psycopg.Connection, agent_id: int, title: str, resolution: str | None) -> int:
    row = conn.execute(
        "INSERT INTO agent_questions (agent_id, title, question, resolved_at, resolution, reply) "
        "VALUES (%s, %s, %s, CASE WHEN %s::text IS NULL THEN NULL ELSE now() END, %s::text, "
        "CASE WHEN %s::text = 'answered' THEN 'yes' END) RETURNING id",
        (agent_id, title, Jsonb({"type": "text"}), resolution, resolution, resolution),
    ).fetchone()
    assert row is not None
    return int(row[0])


def test_only_the_agents_own_unresolved_questions_are_read_in_id_order(
    db_conn: psycopg.Connection,
) -> None:
    mine, other = create_agent(db_conn), create_agent(db_conn)
    first = _ask(db_conn, mine, "first", None)
    _ask(db_conn, mine, "answered", "answered")
    _ask(db_conn, mine, "dismissed", "dismissed")
    _ask(db_conn, mine, "closed", "withdrawn")
    second = _ask(db_conn, mine, "second", None)
    _ask(db_conn, other, "theirs", None)
    db_conn.commit()
    assert [(q.id, q.title) for q in read_open_questions(db_conn, mine)] == [
        (first, "first"),
        (second, "second"),
    ]
    assert [q.title for q in read_open_questions(db_conn, other)] == ["theirs"]
    assert read_open_questions(db_conn, create_agent(db_conn)) == []
