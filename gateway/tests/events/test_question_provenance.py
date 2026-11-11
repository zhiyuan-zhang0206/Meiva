import psycopg

from base.api_contracts.idempotency import keyed_headers
from gateway.tests.auth.gateway_config import gateway_test_client

# meiva:note
# A question row carries the credential the SERVER verified, not the caller's claim:
# the path's agent_id says who the caller says it is, this says what proved it.
# meiva:link gateway/agents/docs/operation-receipts.md


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


# meiva:note
# The ask path stores the fact the auth middleware verified; the test client
# presents the human cluster secret, which the middleware records as cluster_bearer.
def test_ask_records_the_verified_credential(db_conn: psycopg.Connection) -> None:
    agent = seed_agent(db_conn)
    with gateway_test_client() as client:
        created = client.post(
            f"/api/agents/{agent}/asks",
            json={
                "questions": [
                    {"title": "which credential posted this", "question": {"type": "text"}}
                ]
            },
            headers=keyed_headers("provenance-create"),
        )
        feed = client.get("/api/questions/open", params={"agent_id": agent}).json()

    assert created.status_code == 201
    with db_conn.cursor() as cur:
        cur.execute("SELECT source_verified_by FROM agent_questions WHERE agent_id = %s", (agent,))
        assert cur.fetchone() == ("cluster_bearer",)
    assert feed[0]["source_verified_by"] == "cluster_bearer"


# meiva:note
# Rows written before the column existed keep NULL, and the feed reports it as
# absent rather than inventing a credential for them.
def test_a_row_without_a_credential_reads_as_null(db_conn: psycopg.Connection) -> None:
    agent = seed_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_questions (agent_id, title, question) VALUES (%s, 'legacy row', '{\"type\": \"text\"}')",
            (agent,),
        )
    db_conn.commit()

    with gateway_test_client() as client:
        feed = client.get("/api/questions/open", params={"agent_id": agent}).json()
    assert feed[0]["source_verified_by"] is None
