import psycopg
import pytest
from psycopg.types.json import Jsonb

from base.db import create_agent
from gateway.tests.auth.gateway_config import gateway_test_client
from meiva import ui
from meiva.gateway_client import use_client
from meiva.tests.fixtures.pin_agent import pin_agent
from meiva.tests.fixtures.replay_sdk_write import replay_sdk_write

pytestmark = pytest.mark.usefixtures("gateway_data_plane", "sdk_identity")


def test_sdk_publication_and_reader_share_the_gateway_contract(db_conn: psycopg.Connection) -> None:
    agent_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta (birth_config, id, spawner, status) VALUES (%s,%s,'test','idling')",
        (Jsonb({"user_delivery_mode": "sdk"}), agent_id),
    )
    db_conn.commit()
    pin_agent(agent_id)
    with gateway_test_client(base_url="http://test-gateway") as client, use_client(client):
        first = replay_sdk_write("first", ui.publish, "First milestone", suggestions=[])
        assert replay_sdk_write("first", ui.publish, "First milestone", suggestions=[]) == first
        second = ui.publish(
            "Choose a scope",
            suggestions=["Small", "Large"],
        )
        records = ui.publications(limit=1)
        assert [record.id for record in records] == [first]
        assert [record.id for record in ui.publications(after_id=first)] == [second]
        assert all(record.read_at is None for record in ui.publications())
        ui.publish(None, suggestions=[])
        assert len(ui.publications(pending_only=False)) == 2


def test_sdk_ask_keeps_multiple_questions_and_agent_note_drafts(
    db_conn: psycopg.Connection,
) -> None:
    agent_id = create_agent(db_conn)
    db_conn.commit()
    pin_agent(agent_id)
    with gateway_test_client(base_url="http://test-gateway") as client, use_client(client):
        first = replay_sdk_write(
            "scope",
            ui.ask,
            "Choose scope",
            question={"type": "single_select", "options": [{"id": "small", "label": "Small"}]},
            note_suggestion="Validate first",
        )
        assert (
            replay_sdk_write(
                "scope",
                ui.ask,
                "Choose scope",
                question={"type": "single_select", "options": [{"id": "small", "label": "Small"}]},
                note_suggestion="Validate first",
            )
            == first
        )
        second = ui.ask("When?", note_suggestion="Tomorrow")
        rows = ui.questions()
        assert {row["id"] for row in rows} == {first, second}
        assert all(row["expire_at"] is None for row in rows)
        assert rows[0]["answer"] is None
    assert db_conn.execute("SELECT count(*) FROM notification_events").fetchone() == (0,)
