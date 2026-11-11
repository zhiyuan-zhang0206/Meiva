from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from base.api_contracts.user_delivery import UserMessage
from base.events.live.bus import EventBus
from gateway.agents.user_delivery.router import UserMessageFeed
from gateway.tests.auth.gateway_config import gateway_test_client
from gateway.tests.events.test_notices_endpoint import seed_agent
from gateway.tests.support.live_events.recorder import recording_live_events

pytestmark = pytest.mark.usefixtures("gateway_data_plane")


def headers(key: str) -> dict[str, str]:
    return {"Idempotency-Key": key, "Idempotency-Scope": "principal-v1"}


def sdk_agent(conn: psycopg.Connection) -> int:
    aid = seed_agent(conn)
    conn.execute(
        "UPDATE agents_meta SET birth_config=%s WHERE id=%s",
        (Jsonb({"user_delivery_mode": "sdk"}), aid),
    )
    conn.commit()
    return aid


def publish_one(client: TestClient, aid: int, key: str) -> UserMessage:
    result = client.post(
        f"/api/agents/{aid}/user-messages",
        json={"content": f"Update {key}", "suggestions": []},
        headers=headers(key),
    )
    assert result.status_code == 201, result.text
    return UserMessage.model_validate(result.json())


def seen_path(aid: int) -> str:
    return f"/api/keyed/v1/agents/{aid}/user-messages/seen"


def test_seen_moves_one_cursor_and_the_feed_reports_what_lies_past_it(
    db_conn: psycopg.Connection,
) -> None:
    aid = sdk_agent(db_conn)
    path = f"/api/agents/{aid}/user-messages"
    with gateway_test_client() as client:
        first, second, third = (publish_one(client, aid, key) for key in ("a", "b", "c"))
        assert client.get(path).json()["seen_id"] == 0
        marked = client.post(seen_path(aid), json={"up_to_id": second.id}, headers=headers("s1"))
        assert marked.status_code == 200, marked.text
        assert marked.json() == {"seen_id": second.id, "advanced": True}
        feed = UserMessageFeed.model_validate(client.get(path).json())
        assert feed.seen_id == second.id and len(feed.items) == 3
        pending = client.get(path + "?pending=true").json()
        assert [item["id"] for item in pending["items"]] == [third.id]
        assert pending["seen_id"] == second.id
        assert first.id < second.id < third.id
        assert "read_at" not in feed.items[0].model_dump()
        old_shape = client.post(seen_path(aid), json={"action": "read"}, headers=headers("old"))
        assert old_shape.status_code == 422
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (aid,)
    ).fetchone() == (0,)


def test_seen_cursor_never_moves_back_and_is_idempotent(db_conn: psycopg.Connection) -> None:
    aid = sdk_agent(db_conn)
    with gateway_test_client() as client:
        first, second = publish_one(client, aid, "a"), publish_one(client, aid, "b")
        body = {"up_to_id": second.id}
        advanced = client.post(seen_path(aid), json=body, headers=headers("same"))
        replay = client.post(seen_path(aid), json=body, headers=headers("same"))
        again = client.post(seen_path(aid), json=body, headers=headers("other-key"))
        lower = client.post(seen_path(aid), json={"up_to_id": first.id}, headers=headers("lower"))
        assert advanced.json() == replay.json() == {"seen_id": second.id, "advanced": True}
        assert again.json() == {"seen_id": second.id, "advanced": False}
        assert lower.json() == {"seen_id": second.id, "advanced": False}
        changed = client.post(seen_path(aid), json={"up_to_id": first.id}, headers=headers("same"))
        assert changed.status_code == 409
    assert db_conn.execute(
        "SELECT seen_id FROM agent_user_seen WHERE agent_id=%s", (aid,)
    ).fetchone() == (second.id,)


def test_seen_touches_only_its_own_agent_and_is_bounded_by_its_deliveries(
    db_conn: psycopg.Connection,
) -> None:
    mine, other = sdk_agent(db_conn), sdk_agent(db_conn)
    with gateway_test_client() as client:
        record = publish_one(client, mine, "a")
        foreign = publish_one(client, other, "a")
        top = max(record.id, foreign.id)
        beyond = client.post(seen_path(mine), json={"up_to_id": top + 1}, headers=headers("beyond"))
        assert beyond.status_code == 422
        marked = client.post(seen_path(mine), json={"up_to_id": record.id}, headers=headers("mine"))
        assert marked.json() == {"seen_id": record.id, "advanced": True}
        assert client.get(f"/api/agents/{other}/user-messages?pending=true").json()["items"] == [
            foreign.model_dump(mode="json")
        ]
        missing = client.post(
            seen_path(other + 1000), json={"up_to_id": 1}, headers=headers("missing")
        )
        assert missing.status_code == 404
    assert db_conn.execute("SELECT agent_id FROM agent_user_seen").fetchall() == [(mine,)]


def test_seen_announces_agent_updated_only_when_the_cursor_moved(
    db_conn: psycopg.Connection, event_bus: EventBus
) -> None:
    aid = sdk_agent(db_conn)
    with gateway_test_client() as client:
        client.app.state.bus = event_bus
        record = publish_one(client, aid, "a")
        with recording_live_events(event_bus) as live:
            moved = client.post(
                seen_path(aid), json={"up_to_id": record.id}, headers=headers("one")
            )
            assert moved.json()["advanced"] is True
            announced = live.settle("agent_updated", 1)
            assert announced[-1].payload["agent_id"] == aid
            still = client.post(
                seen_path(aid), json={"up_to_id": record.id}, headers=headers("two")
            )
            assert still.json()["advanced"] is False
            live.quiet()
            assert len(live.events("agent_updated")) == 1


@pytest.mark.parametrize("body", [{"up_to_id": 0}, {"up_to_id": -3}, {}, {"up_to_id": "x"}])
def test_seen_rejects_invalid_bodies(db_conn: psycopg.Connection, body: dict[str, object]) -> None:
    aid = sdk_agent(db_conn)
    with gateway_test_client() as client:
        response = client.post(seen_path(aid), json=body, headers=headers("bad"))
        assert response.status_code == 422


def test_seen_requires_an_idempotency_key(db_conn: psycopg.Connection) -> None:
    aid = sdk_agent(db_conn)
    with gateway_test_client() as client:
        assert client.post(seen_path(aid), json={"up_to_id": 1}).status_code == 422


def test_roster_flags_unread_only_for_deliveries_past_the_cursor(
    db_conn: psycopg.Connection,
) -> None:
    aid = sdk_agent(db_conn)
    with gateway_test_client() as client:
        silent = client.post(
            f"/api/agents/{aid}/user-messages", json={"content": None}, headers=headers("silent")
        )
        assert silent.status_code == 201

        def unread() -> bool:
            card = next(
                row for row in client.get("/api/agents").json()["agents"] if row["agent_id"] == aid
            )
            return bool(card["has_unread_user_messages"])

        assert unread() is False
        record = publish_one(client, aid, "a")
        assert unread() is True
        client.post(seen_path(aid), json={"up_to_id": record.id}, headers=headers("seen"))
        assert unread() is False
        publish_one(client, aid, "b")
        assert unread() is True


def test_publish_retry_is_atomic_and_rejects_changed_content(db_conn: psycopg.Connection) -> None:
    aid = sdk_agent(db_conn)
    path = f"/api/agents/{aid}/user-messages"
    body: dict[str, object] = {"content": "Result", "suggestions": []}
    with gateway_test_client() as client, ThreadPoolExecutor(max_workers=4) as pool:
        calls = [
            pool.submit(client.post, path, json=body, headers=headers("same")) for _ in range(4)
        ]
        responses = [future.result() for future in calls]
        assert all(response.status_code == 201 for response in responses), [
            r.text for r in responses
        ]
        assert len({response.json()["id"] for response in responses}) == 1
        assert (
            client.post(
                path, json=body | {"content": "Different"}, headers=headers("same")
            ).status_code
            == 409
        )
    assert db_conn.execute(
        "SELECT count(*) FROM agent_user_messages WHERE agent_id=%s", (aid,)
    ).fetchone() == (1,)


@pytest.mark.parametrize(
    "body",
    [
        {"content": "Question", "response": "required"},
        {"content": "", "response": "none", "suggestions": []},
        {"content": "Question", "response": "surprise", "suggestions": []},
        {"content": None, "response": "required", "suggestions": []},
        {"content": "Result", "response": "none", "suggestions": ["Approve"]},
    ],
)
def test_bad_publication_is_rejected(db_conn: psycopg.Connection, body: dict[str, object]) -> None:
    aid = sdk_agent(db_conn)
    with gateway_test_client() as client:
        response = client.post(
            f"/api/agents/{aid}/user-messages", json=body, headers=headers("invalid")
        )
        assert response.status_code == 422
    assert db_conn.execute("SELECT count(*) FROM agent_user_messages").fetchone() == (0,)


def test_sdk_has_one_delivery_entry_and_explicit_silence(db_conn: psycopg.Connection) -> None:
    aid = sdk_agent(db_conn)
    with gateway_test_client() as client:
        response = client.post(f"/api/agents/{aid}/reply-suggestion")
        assert response.status_code == 200 and response.json()["reason"] == "no_agent_text", (
            response.text
        )
        response = client.post(
            f"/api/agents/{aid}/user-messages",
            json={"content": None},
            headers=headers("no-update"),
        )
        assert response.status_code == 201, response.text
        assert client.get(f"/api/agents/{aid}/user-messages").json()["items"] == []
    assert db_conn.execute("SELECT count(*) FROM agent_user_messages").fetchone() == (1,)


def test_ask_retries_note_and_answer_share_one_receipt(db_conn: psycopg.Connection) -> None:
    aid = sdk_agent(db_conn)
    path = f"/api/agents/{aid}/asks"
    question = {
        "title": "Choose scope",
        "question": {
            "type": "multi_select",
            "options": [{"id": "small", "label": "Small"}],
            "note_suggestion": "Validate first",
        },
    }
    with gateway_test_client() as client:
        created = client.post(path, json=question, headers=headers("question"))
        assert created.status_code == 201, created.text
        question_id = created.json()["id"]
        row = client.get(f"/api/notices/open?agent_id={aid}").json()[0]
        assert row["answer"] is None
        assert row["question"]["note_suggestion"] == "Validate first"
        resolve = f"/api/keyed/v1/agents/{aid}/notices/{question_id}/resolve"
        body = {
            "action": "answer",
            "answer": {"option_ids": ["small"], "comment": "Validate tonight"},
        }
        answer = client.post(resolve, json=body, headers=headers("answer"))
        assert answer.status_code == 201, answer.text
        assert client.post(resolve, json=body, headers=headers("answer")).json() == answer.json()
        assert client.get(f"/api/notices/open?agent_id={aid}").json() == []
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (aid,)
    ).fetchone() == (1,)
    row = db_conn.execute(
        "SELECT content FROM inbound_messages WHERE agent_id=%s", (aid,)
    ).fetchone()
    assert row is not None and "Validate tonight" in row[0]


def test_feedback_requires_an_offered_candidate_and_records_explicit_intent(
    db_conn: psycopg.Connection,
) -> None:
    from gateway.agents.reply_suggestion.feedback import save_offer

    aid = sdk_agent(db_conn)
    offer = save_offer(db_conn, aid, "Release", ["Ship", "Wait"])
    db_conn.commit()
    path = f"/api/agents/{aid}/reply-suggestion/feedback"
    body = {
        "offer_id": offer,
        "candidate_index": 0,
        "outcome": "edited",
        "sent_text": "Ship to staging",
    }
    with gateway_test_client() as client:
        assert client.post(path, json=body, headers=headers("feedback")).status_code == 200
        assert client.post(path, json=body, headers=headers("feedback")).status_code == 200
        assert (
            client.post(
                path, json=body | {"outcome": "accepted"}, headers=headers("invalid")
            ).status_code
            == 422
        )
        assert (
            client.post(
                path, json=body | {"candidate_index": 9}, headers=headers("missing")
            ).status_code
            == 422
        )
        assert (
            client.post(
                path,
                json={"offer_id": offer, "candidate_index": 1, "outcome": "rejected"},
                headers=headers("reject"),
            ).status_code
            == 200
        )
    assert db_conn.execute(
        "SELECT outcome,sent_text FROM reply_suggestion_feedback ORDER BY id"
    ).fetchall() == [("edited", "Ship to staging"), ("rejected", None)]


def test_ask_creation_retry_preserves_each_independent_question(
    db_conn: psycopg.Connection,
) -> None:
    aid = sdk_agent(db_conn)
    path = f"/api/agents/{aid}/asks"
    question = {"title": "Choose scope", "question": {"type": "text"}}
    with gateway_test_client() as client, ThreadPoolExecutor(max_workers=3) as pool:
        calls = [
            pool.submit(client.post, path, json=question, headers=headers("question"))
            for _ in range(3)
        ]
        results = [call.result() for call in calls]
        assert all(result.status_code == 201 for result in results), results
        assert len({result.json()["id"] for result in results}) == 1
        changed = question | {"title": "Choose timing"}
        assert client.post(path, json=changed, headers=headers("question")).status_code == 409
        assert client.post(path, json=changed, headers=headers("timing")).status_code == 201
        assert len(client.get(f"/api/notices/open?agent_id={aid}").json()) == 2


def unread_pushes(conn: psycopg.Connection, aid: int) -> list[tuple[str, str, str | None]]:
    rows = conn.execute(
        "SELECT action, subject, body FROM notification_events "
        "WHERE agent_id=%s AND category='core.unread' ORDER BY id",
        (aid,),
    ).fetchall()
    return [(row[0], row[1], row[2]) for row in rows]


def test_sdk_publication_stages_one_unread_push_that_the_seen_cursor_retracts(
    db_conn: psycopg.Connection,
) -> None:
    aid = sdk_agent(db_conn)
    with gateway_test_client() as client:
        publish_one(client, aid, "a")
        newest = publish_one(client, aid, "b")
        assert unread_pushes(db_conn, aid) == [
            ("alert", f"#{aid}", "Update a"),
            ("replace", f"#{aid}", "Update b"),
        ]
        marked = client.post(seen_path(aid), json={"up_to_id": newest.id}, headers=headers("s"))
        assert marked.status_code == 200, marked.text
        replay = client.post(seen_path(aid), json={"up_to_id": newest.id}, headers=headers("s2"))
        assert replay.json()["advanced"] is False
    assert [action for action, *_ in unread_pushes(db_conn, aid)] == ["alert", "replace", "retract"]
