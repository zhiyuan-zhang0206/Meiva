import os
import secrets
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

from base.db import create_agent
from gateway import app as gateway_app
from gateway.tests.auth.gateway_config import gateway_test_client
from meiva import ui
from meiva.gateway_client import use_client
from meiva.sdk_surface import agent_identity, process_context
from meiva.self.harness.attachment import HarnessAttachment, open_attachment
from meiva.self.harness.registration import HarnessRegistration, handle_digest
from meiva.self.harness.store import registration_store
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
    items: list[dict[str, object]] = [
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


@contextmanager
def registered_harness(agent_id: int, db_url: str) -> Generator[HarnessAttachment]:
    from langgraph.checkpoint.base import empty_checkpoint
    from langgraph.checkpoint.postgres import PostgresSaver

    with PostgresSaver.from_conn_string(db_url) as saver:
        saver.put(
            config={"configurable": {"thread_id": str(agent_id), "checkpoint_ns": ""}},
            checkpoint=empty_checkpoint(),
            metadata={"source": "input", "step": 1, "parents": {}},
            new_versions={},
        )
    pin_agent(None, owns_loop=False)
    context = process_context.current_context()
    handle = secrets.token_urlsafe(32)
    store = registration_store(context.require_meiva_home())
    store.save(
        HarnessRegistration(
            agent_id=agent_id,
            harness="codex",
            external_session_id="isolated-ui-first",
            machine=context.require_machine().name(),
            controller_pid=os.getpid(),
            handle_sha256=handle_digest(handle),
            created_at=datetime.now(UTC).isoformat(),
            expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        )
    )
    try:
        with open_attachment(handle, agent_id=agent_id) as attachment:
            bound = process_context.current_context()
            assert bound.identity is not None
            assert bound.identity.agent_id is None
            assert bound.identity.lease is not None
            assert agent_identity.require_agent_id(bound) == agent_id
            yield attachment
    finally:
        store.delete(agent_id)


def assert_isolated_worktree_sources() -> None:
    root = Path(__file__).resolve().parents[3]
    assert ui.__file__ is not None and Path(ui.__file__).resolve().is_relative_to(root)
    assert gateway_app.__file__ is not None and Path(gateway_app.__file__).resolve().is_relative_to(
        root
    )
    assert (
        process_context.current_context().require_meiva_home().name.startswith("meiva_test_home_")
    )


@pytest.mark.parametrize("external", [False, True], ids=["native", "registered-harness"])
def test_ui_first_batch_is_persisted_for_the_admitted_caller(
    db_conn: psycopg.Connection, db_url: str, external: bool
) -> None:
    assert_isolated_worktree_sources()
    agent_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta (birth_config, id, spawner, status) VALUES (%s,%s,'test','idling')",
        (Jsonb({}), agent_id),
    )
    other_id = create_agent(db_conn)
    db_conn.commit()
    items: list[dict[str, object]] = [
        {"title": "First isolated question"},
        {"title": "Second isolated question"},
    ]
    with ExitStack() as stack:
        if external:
            attachment = stack.enter_context(registered_harness(agent_id, db_url))
            assert attachment.agent_id == agent_id
        else:
            pin_agent(agent_id)
        client = stack.enter_context(gateway_test_client(base_url="http://test-gateway"))
        stack.enter_context(use_client(client))
        first, second = replay_sdk_write("ui-first-persisted", ui.ask, items)
        assert replay_sdk_write("ui-first-persisted", ui.ask, items) == [first, second]
        assert first < second
        for _ in range(2):
            response = client.get("/api/questions/open", params={"agent_id": agent_id})
            assert response.status_code == 200
            assert [(row["id"], row["agent_id"], row["title"]) for row in response.json()] == [
                (first, agent_id, items[0]["title"]),
                (second, agent_id, items[1]["title"]),
            ]
            assert client.get("/api/questions/open", params={"agent_id": other_id}).json() == []
        assert db_conn.execute(
            "SELECT id, agent_id, title FROM agent_questions ORDER BY id"
        ).fetchall() == [
            (first, agent_id, items[0]["title"]),
            (second, agent_id, items[1]["title"]),
        ]
        ui.close_question(first)
        ui.close_question(second)
        assert client.get("/api/questions/open", params={"agent_id": agent_id}).json() == []
