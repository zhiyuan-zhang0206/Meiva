import psycopg

from base.api_contracts.idempotency import keyed_headers
from gateway.tests.auth.gateway_config import gateway_test_client

# meiva:note
# A notice posted from a schedule's own process records which schedule that was, so the row
# answers "which schedule sent this" without reading that process's environment after the fact.
# The caller reports the id; the server refuses one that names no schedule row.
# meiva:link gateway/agents/docs/notice-receipts.md


def seed_agent(db_conn: psycopg.Connection) -> int:
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        row = cur.fetchone()
        assert row is not None
        new_id = row[0]
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', 'idling')",
            (new_id,),
        )
    db_conn.commit()
    return new_id


def seed_schedule(db_conn: psycopg.Connection, name: str) -> int:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schedules (name, script, command, machine) "
            "VALUES (%s, 'print(1)', 'python schedule.py', 'macmini') RETURNING id",
            (name,),
        )
        row = cur.fetchone()
    db_conn.commit()
    assert row is not None
    return row[0]


def test_notice_create_records_the_schedule_that_posted_it(db_conn: psycopg.Connection) -> None:
    agent = seed_agent(db_conn)
    schedule = seed_schedule(db_conn, "notice-schedule-provenance")
    with gateway_test_client() as client:
        created = client.post(
            f"/api/agents/{agent}/notices",
            json={
                "title": "which schedule posted this",
                "require_response": True,
                "schedule_id": schedule,
            },
            headers=keyed_headers("schedule-provenance-create"),
        )
    assert created.status_code == 201, created.text
    with db_conn.cursor() as cur:
        cur.execute("SELECT schedule_id FROM agent_notices WHERE agent_id = %s", (agent,))
        assert cur.fetchone() == (schedule,)


# meiva:note
# Omission is "not known", not an error: an agent's own notice and a manual run have no
# schedule, and the row keeps NULL for them.
def test_an_omitted_schedule_stays_null(db_conn: psycopg.Connection) -> None:
    agent = seed_agent(db_conn)
    with gateway_test_client() as client:
        created = client.post(
            f"/api/agents/{agent}/notices",
            json={"title": "my own notice", "require_response": True},
            headers=keyed_headers("schedule-provenance-omitted"),
        )
    assert created.status_code == 201, created.text
    with db_conn.cursor() as cur:
        cur.execute("SELECT schedule_id FROM agent_notices WHERE agent_id = %s", (agent,))
        assert cur.fetchone() == (None,)


# meiva:note
# The server accepts the value rather than reading an environment it cannot see, and it refuses
# a schedule that does not exist: a wrong id would make the row lie about where it came from.
def test_an_unknown_schedule_is_refused(db_conn: psycopg.Connection) -> None:
    agent = seed_agent(db_conn)
    with gateway_test_client() as client:
        refused = client.post(
            f"/api/agents/{agent}/notices",
            json={"title": "from nowhere", "require_response": True, "schedule_id": 987654},
            headers=keyed_headers("schedule-provenance-unknown"),
        )
    assert refused.status_code == 422, refused.text
    assert "does not exist" in refused.text
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agent_notices WHERE agent_id = %s", (agent,))
        assert cur.fetchone() == (0,)
