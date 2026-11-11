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


def test_sdk_publish_is_replayed_by_its_runtime_key(db_conn: psycopg.Connection) -> None:
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
        assert ui.publish("Choose a scope", suggestions=["Small", "Large"]) != first
        ui.publish(None, suggestions=[])
    assert db_conn.execute(
        "SELECT count(*) FROM agent_publications WHERE agent_id=%s", (agent_id,)
    ).fetchone() == (3,)


def test_sdk_ask_takes_a_list_of_questions_and_each_can_be_closed(
    db_conn: psycopg.Connection,
) -> None:
    agent_id = create_agent(db_conn)
    db_conn.commit()
    pin_agent(agent_id)
    items = [
        {
            "title": "Choose scope",
            "type": "single_select",
            "options": [{"id": "small", "label": "Small"}],
            "note_suggestion": "Validate first",
        },
        {"title": "When?", "note_suggestion": "Tomorrow"},
    ]
    with gateway_test_client(base_url="http://test-gateway") as client, use_client(client):
        first, second = replay_sdk_write("scope", ui.ask, items)
        assert replay_sdk_write("scope", ui.ask, items) == [first, second]
        assert first < second
        [third] = ui.ask([{"title": "Later?"}])
        with pytest.raises(ValueError, match="non-empty"):
            ui.ask([])
        with pytest.raises(ValueError, match="unknown keys"):
            ui.ask([{"title": "x", "question": {"type": "text"}}])
        ui.close_question(first)
        replay_sdk_write("close", ui.close_question, second)
        replay_sdk_write("close", ui.close_question, second)
        ui.close_question(third)
        with pytest.raises(LookupError):
            ui.close_question(third + 1000)
    assert db_conn.execute(
        "SELECT action, subject FROM notification_events WHERE category = 'core.question' "
        "ORDER BY id"
    ).fetchall() == [
        ("alert", "2 questions awaiting an answer"),
        ("replace", "3 questions awaiting an answer"),
        ("replace", "2 questions awaiting an answer"),
        ("replace", "Later?"),
        ("retract", "Questions answered"),
    ]
    assert db_conn.execute(
        "SELECT resolution FROM agent_questions WHERE agent_id=%s ORDER BY id", (agent_id,)
    ).fetchall() == [("withdrawn",), ("withdrawn",), ("withdrawn",)]
