import psycopg
import pytest
from psycopg.types.json import Jsonb

from base.db import create_agent
from gateway.tests.auth.gateway_config import gateway_test_client
from meiva import ui
from meiva.gateway_client import use_client
from meiva.tests.fixtures.pin_agent import pin_agent

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
        first = ui.publish(
            "First milestone", response="none", suggestions=[], idempotency_key="first"
        )
        assert (
            ui.publish("First milestone", response="none", suggestions=[], idempotency_key="first")
            == first
        )
        second = ui.publish(
            "Choose a scope",
            response="required",
            suggestions=["Small", "Large"],
            idempotency_key="question",
        )
        records = ui.publications(limit=1)
        assert [record.id for record in records] == [first]
        assert [record.id for record in ui.publications(after_id=first)] == [second]
        assert all(record.read_at is None for record in ui.publications())
        ui.publish(None, response="none", suggestions=[], idempotency_key="quiet")
        assert len(ui.publications(pending_only=False)) == 2
