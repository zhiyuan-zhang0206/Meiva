import psycopg

from base.api_contracts.idempotency import keyed_headers
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.daemon.schedules.completion_notices import (
    current_default_completion_notice_policy,
    policy_for_agent,
)
from gateway.tests.auth.gateway_config import gateway_test_client


def _seed_agent(
    db_conn: psycopg.Connection, status: str = "idling", *, local_machine: MachineIdentityReader
) -> int:
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        row = cur.fetchone()
        assert row is not None
        new_id = row[0]
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status, machine) VALUES (%s, 'test', %s, %s)",
            (new_id, status, local_machine.name()),
        )
    db_conn.commit()
    return new_id


def _pending_rows(conn: psycopg.Connection, agent_id: int) -> list[tuple[str, str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT kind, status, content FROM inbound_messages WHERE agent_id = %s ORDER BY id",
            (agent_id,),
        )
        return cur.fetchall()


def test_post_message_inserts_chat_pending(
    db_conn: psycopg.Connection, *, machine_identity: MachineIdentityReader
) -> None:
    tid = _seed_agent(db_conn, local_machine=machine_identity)
    with gateway_test_client(machine=machine_identity) as client:
        resp = client.post(
            f"/api/agents/{tid}/messages", json={"content": "hi agent", "source": "user"}
        )
    assert resp.status_code == 201
    assert "status" in resp.json()
    assert _pending_rows(db_conn, tid) == [("chat", "pending", "hi agent")]


def test_hourly_completion_policy_buffers_every_notice(
    db_conn: psycopg.Connection,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentityReader,
) -> None:
    tid = _seed_agent(db_conn, local_machine=machine_identity)
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET config_overlay = %s::jsonb WHERE id = %s",
            ('{"completion_notice_policy": "hourly"}', tid),
        )
    db_conn.commit()
    policy = policy_for_agent(
        db_conn, tid, current_default_completion_notice_policy(authority=config_authority)
    )
    with gateway_test_client(machine=machine_identity) as client:
        first = client.post(
            f"/api/agents/{tid}/messages",
            json={
                "content": "Background command 'build' finished. Full output at log.",
                "source": "shell:55",
                "completion_notice": True,
            },
        )
        second = client.post(
            f"/api/agents/{tid}/messages",
            json={
                "content": "Background command 'test' finished. Full output at log.",
                "source": "shell:56",
                "completion_notice": True,
            },
        )
    assert policy == "hourly"
    assert first.status_code == 201 and first.json()["inbound_id"] is None
    assert second.status_code == 201 and second.json()["inbound_id"] is None
    assert len(_pending_rows(db_conn, tid)) == 0
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM completion_notice_events WHERE agent_id = %s",
            (tid,),
        )
        assert cur.fetchone() == (2,)


def test_default_completion_policy_preserves_every_completion(
    db_conn: psycopg.Connection, *, machine_identity: MachineIdentityReader
) -> None:
    tid = _seed_agent(db_conn, local_machine=machine_identity)
    with gateway_test_client(machine=machine_identity) as client:
        response = client.post(
            f"/api/agents/{tid}/messages",
            json={
                "content": "Background command 'default' finished. Full output at log.",
                "source": "shell:59",
                "completion_notice": True,
            },
        )
    assert response.status_code == 201 and response.json()["inbound_id"] is not None
    assert len(_pending_rows(db_conn, tid)) == 1


def test_post_message_empty_content_422(
    db_conn: psycopg.Connection, *, machine_identity: MachineIdentityReader
) -> None:
    tid = _seed_agent(db_conn, local_machine=machine_identity)
    with gateway_test_client(machine=machine_identity) as client:
        resp = client.post(f"/api/agents/{tid}/messages", json={"content": "   ", "source": "user"})
    assert resp.status_code == 422
    assert _pending_rows(db_conn, tid) == []


def test_post_message_large_content_is_accepted(
    db_conn: psycopg.Connection, *, machine_identity: MachineIdentityReader
) -> None:
    tid = _seed_agent(db_conn, local_machine=machine_identity)
    content = "a" * 100_000
    with gateway_test_client(machine=machine_identity) as client:
        resp = client.post(
            f"/api/agents/{tid}/messages", json={"content": content, "source": "user"}
        )
    assert resp.status_code == 201
    assert _pending_rows(db_conn, tid) == [("chat", "pending", content)]


def test_post_message_content_at_one_mib_is_accepted(
    db_conn: psycopg.Connection, *, machine_identity: MachineIdentityReader
) -> None:
    tid = _seed_agent(db_conn, local_machine=machine_identity)
    content = "a" * 1_048_576
    with gateway_test_client(machine=machine_identity) as client:
        resp = client.post(
            f"/api/agents/{tid}/messages", json={"content": content, "source": "user"}
        )
    assert resp.status_code == 201
    assert _pending_rows(db_conn, tid) == [("chat", "pending", content)]


def test_post_message_content_over_one_mib_is_413(
    db_conn: psycopg.Connection, *, machine_identity: MachineIdentityReader
) -> None:
    tid = _seed_agent(db_conn, local_machine=machine_identity)
    content = "é" * 524_289
    assert len(content.encode("utf-8")) > 1_048_576
    with gateway_test_client(machine=machine_identity) as client:
        resp = client.post(
            f"/api/agents/{tid}/messages", json={"content": content, "source": "user"}
        )
    assert resp.status_code == 413
    assert "1 MiB" in resp.json()["detail"]
    assert "file path" in resp.json()["detail"]
    assert _pending_rows(db_conn, tid) == []


def test_reconcile_rejects_over_limit_after_timeout(
    db_conn: psycopg.Connection, *, machine_identity: MachineIdentityReader
) -> None:
    tid = _seed_agent(db_conn, local_machine=machine_identity)
    content = "é" * 524_289
    with gateway_test_client(machine=machine_identity) as client:
        resp = client.post(
            f"/api/agents/{tid}/messages/reconcile",
            json={"content": content, "source": "user"},
            headers=keyed_headers("k1"),
        )
    assert resp.status_code == 413
    assert _pending_rows(db_conn, tid) == []


def test_post_message_404_when_agent_missing(db_conn: psycopg.Connection) -> None:
    with gateway_test_client() as client:
        resp = client.post("/api/agents/9999/messages", json={"content": "x", "source": "user"})
    assert resp.status_code == 404
    assert _pending_rows(db_conn, 9999) == []


def test_post_message_terminated_agent_allows_insert(
    db_conn: psycopg.Connection, *, machine_identity: MachineIdentityReader
) -> None:
    tid = _seed_agent(db_conn, status="terminated", local_machine=machine_identity)
    db_conn.execute(
        "UPDATE agents_meta SET runtime_generation = gen_random_uuid(), "
        "runtime_owner = gen_random_uuid() WHERE id = %s",
        (tid,),
    )
    db_conn.commit()
    with gateway_test_client(machine=machine_identity) as client:
        resp = client.post(f"/api/agents/{tid}/messages", json={"content": "x", "source": "user"})
    assert resp.status_code == 201
    assert "status" in resp.json()
    rows = _pending_rows(db_conn, tid)
    assert rows[0] == ("chat", "pending", "x")
    assert rows[1] == ("resurrect", "pending", "")


def test_post_message_command_chain_is_one_inbound(
    db_conn: psycopg.Connection, *, machine_identity: MachineIdentityReader
) -> None:
    tid = _seed_agent(db_conn, local_machine=machine_identity)
    with gateway_test_client(machine=machine_identity) as client:
        resp = client.post(
            f"/api/agents/{tid}/messages",
            json={"content": "/recap the week /compact", "source": "user"},
        )
    assert resp.status_code == 201
    assert _pending_rows(db_conn, tid) == [("chat", "pending", "/recap the week /compact")]


def test_post_message_does_not_inspect_the_command_chain(
    db_conn: psycopg.Connection, *, machine_identity: MachineIdentityReader
) -> None:
    tid = _seed_agent(db_conn, local_machine=machine_identity)
    with gateway_test_client(machine=machine_identity) as client:
        resp = client.post(
            f"/api/agents/{tid}/messages",
            json={"content": "/compact /recap the week", "source": "user"},
        )
    assert resp.status_code == 201
    assert _pending_rows(db_conn, tid) == [("chat", "pending", "/compact /recap the week")]


def test_post_message_invalid_source_422(
    db_conn: psycopg.Connection, *, machine_identity: MachineIdentityReader
) -> None:
    tid = _seed_agent(db_conn, local_machine=machine_identity)
    with gateway_test_client(machine=machine_identity) as client:
        resp = client.post(f"/api/agents/{tid}/messages", json={"content": "x", "source": "ui:web"})
    assert resp.status_code == 422
    assert "Unrecognized inbound source" in resp.text
    assert _pending_rows(db_conn, tid) == []


def _payload_row(conn: psycopg.Connection, agent_id: int) -> tuple[str, dict[str, object] | None]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT content, payload FROM inbound_messages WHERE agent_id = %s ORDER BY id",
            (agent_id,),
        )
        row = cur.fetchone()
        assert row is not None
        return row[0], row[1]


class TestMessageContentContract:
    def test_content_block_list_rejected_422(
        self, db_conn: psycopg.Connection, *, machine_identity: MachineIdentityReader
    ) -> None:
        tid = _seed_agent(db_conn, local_machine=machine_identity)
        with gateway_test_client(machine=machine_identity) as client:
            resp = client.post(
                f"/api/agents/{tid}/messages",
                json={
                    "content": [
                        {"type": "text", "text": "what is this"},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"/api/agents/{tid}/uploads/a.png"},
                        },
                    ],
                    "source": "user",
                },
            )
        assert resp.status_code == 422
        assert _pending_rows(db_conn, tid) == []

    def test_plain_string_still_works(
        self, db_conn: psycopg.Connection, *, machine_identity: MachineIdentityReader
    ) -> None:
        tid = _seed_agent(db_conn, local_machine=machine_identity)
        with gateway_test_client(machine=machine_identity) as client:
            resp = client.post(
                f"/api/agents/{tid}/messages", json={"content": "hi", "source": "user"}
            )
        assert resp.status_code == 201
        content, payload = _payload_row(db_conn, tid)
        assert content == "hi"
        assert payload is None


def test_quote_send_persists_snapshot_and_reconciles_after_source_changes(
    db_conn: psycopg.Connection, *, machine_identity: MachineIdentityReader
) -> None:
    from langchain_core.messages import AIMessage

    from base.config import settings
    from gateway.tests.support.checkpoints.stored import put_checkpoint

    tid = _seed_agent(db_conn, local_machine=machine_identity)
    put_checkpoint(
        settings.data_plane.db_url,
        tid,
        [AIMessage(id="quote-source", content="**rendered** `code`")],
        version="1",
    )
    quote_body: dict[str, object] = {
        "source_agent_id": tid,
        "source_item_id": "0.0",
        "source_message_id": "quote-source",
        "author": "Agent",
        "text": "rendered code",
    }
    body: dict[str, object] = {"content": "Explain this", "source": "user", "quote": quote_body}
    headers = keyed_headers("quote-context-test")
    with gateway_test_client(machine=machine_identity) as client:
        response = client.post(f"/api/agents/{tid}/messages", json=body, headers=headers)
        assert response.status_code == 201, response.text
        inbound = response.json()["inbound_id"]
        row = db_conn.execute(
            "SELECT content, payload FROM inbound_messages WHERE id=%s", (inbound,)
        ).fetchone()
        assert row is not None
        assert row[0] == body["content"]
        assert row[1]["quote"]["text"] == "rendered code"
        put_checkpoint(
            settings.data_plane.db_url,
            tid,
            [AIMessage(id="replacement", content="replacement")],
            version="2",
        )
        replay = client.post(f"/api/agents/{tid}/messages/reconcile", json=body, headers=headers)
        assert replay.status_code == 200, replay.text
        assert replay.json()["inbound_id"] == inbound
        changed = {**body, "quote": {**quote_body, "text": "different selection"}}
        conflict = client.post(
            f"/api/agents/{tid}/messages/reconcile", json=changed, headers=headers
        )
        assert conflict.status_code == 409
        missing = client.post(f"/api/agents/{tid}/messages", json=body)
        assert missing.status_code == 422
        foreign = {**body, "quote": {**quote_body, "source_agent_id": tid + 1}}
        denied = client.post(f"/api/agents/{tid}/messages", json=foreign)
        assert denied.status_code == 422


def test_quote_accepts_exact_retained_native_segment(
    db_conn: psycopg.Connection, *, machine_identity: MachineIdentityReader
) -> None:
    from langchain_core.messages import AIMessage, SystemMessage

    from base.config import settings
    from gateway.tests.support.checkpoints.stored import put_checkpoint

    tid = _seed_agent(db_conn, local_machine=machine_identity)
    boundary = put_checkpoint(
        settings.data_plane.db_url,
        tid,
        [SystemMessage(content="system"), AIMessage(id="retained-source", content="old **text**")],
        version="1",
        boundary=True,
    )
    put_checkpoint(
        settings.data_plane.db_url, tid, [AIMessage(id="current", content="current")], version="2"
    )
    with gateway_test_client(machine=machine_identity) as client:
        response = client.post(
            f"/api/agents/{tid}/messages",
            json={
                "content": "Explain old text",
                "source": "user",
                "quote": {
                    "source_agent_id": tid,
                    "source_item_id": f"s1.{boundary}.0.0",
                    "source_message_id": "retained-source",
                    "author": "Agent",
                    "text": "old text",
                },
            },
        )
    assert response.status_code == 201, response.text


def test_quote_accepts_readable_marker_with_exact_stable_identity(
    db_conn: psycopg.Connection, *, machine_identity: MachineIdentityReader
) -> None:
    from langchain_core.messages import HumanMessage

    from base.config import settings
    from gateway.tests.support.checkpoints.stored import put_checkpoint

    tid = _seed_agent(db_conn, local_machine=machine_identity)
    _ = put_checkpoint(
        settings.data_plane.db_url,
        tid,
        [
            HumanMessage(
                id="notification-source",
                content="<system_note>Connection failed: retry pending.</system_note>",
                additional_kwargs={"meiva_msg_type": "system_note", "meiva_note_tag": "task"},
            )
        ],
        version="1",
    )
    quote: dict[str, object] = {
        "source_agent_id": tid,
        "source_item_id": "0.0",
        "source_message_id": "notification-source",
        "source_block_idx": 0,
        "author": "Notification",
        "text": "retry pending.",
    }
    with gateway_test_client(machine=machine_identity) as client:
        timeline = client.get(f"/api/agents/{tid}/timeline").json()["items"]
        marker = next(item for item in timeline if item["item_id"] == "0.0")
        assert marker["kind"] == "system_marker"
        assert marker["source_message_id"] == quote["source_message_id"]
        response = client.post(
            f"/api/agents/{tid}/messages",
            json={"content": "Explain retry", "source": "user", "quote": quote},
        )
        assert response.status_code == 201, response.text
        forged = {**quote, "source_message_id": "another-notification"}
        denied = client.post(
            f"/api/agents/{tid}/messages",
            json={"content": "Explain retry", "source": "user", "quote": forged},
        )
        assert denied.status_code == 422
        _ = put_checkpoint(
            settings.data_plane.db_url,
            tid,
            [
                HumanMessage(
                    content="A legacy marker without a durable identity",
                    additional_kwargs={"meiva_msg_type": "system_note", "meiva_note_tag": "task"},
                )
            ],
            version="2",
        )
        anonymous = {**quote, "source_message_id": None, "source_block_idx": None}
        unavailable = client.post(
            f"/api/agents/{tid}/messages",
            json={"content": "Explain retry", "source": "user", "quote": anonymous},
        )
        assert unavailable.status_code == 422


def test_new_quote_post_rejects_eval_isolated_result_reader(
    db_conn: psycopg.Connection, *, machine_identity: MachineIdentityReader
) -> None:
    tid = _seed_agent(db_conn, local_machine=machine_identity)
    caller = _seed_agent(db_conn, local_machine=machine_identity)
    _ = db_conn.execute(
        "UPDATE agents_meta SET config_overlay = %s::jsonb WHERE id = %s",
        ('{"eval_isolation": true}', caller),
    )
    db_conn.commit()
    with gateway_test_client(machine=machine_identity) as client:
        response = client.post(
            f"/api/agents/{tid}/messages",
            params={"caller": f"agent:{caller}"},
            json={
                "content": "Explain",
                "source": "user",
                "quote": {
                    "source_agent_id": tid,
                    "source_item_id": "0.0",
                    "author": "Agent",
                    "text": "selection",
                },
            },
        )
    assert response.status_code == 403, response.text
    assert "eval-isolated" in response.json()["detail"]
    assert _pending_rows(db_conn, tid) == []


def test_quote_post_rejects_unretained_and_out_of_depth_checkpoints(
    db_conn: psycopg.Connection, *, machine_identity: MachineIdentityReader
) -> None:
    from langchain_core.messages import AIMessage, SystemMessage

    from base.config import settings
    from gateway.tests.auth.gateway_config import reconfigure
    from gateway.tests.support.checkpoints.stored import put_checkpoint

    tid = _seed_agent(db_conn, local_machine=machine_identity)
    older = put_checkpoint(
        settings.data_plane.db_url,
        tid,
        [SystemMessage(content="system"), AIMessage(id="old-source", content="old")],
        version="1",
        boundary=True,
    )
    _ = put_checkpoint(
        settings.data_plane.db_url,
        tid,
        [SystemMessage(content="system"), AIMessage(id="newer-source", content="newer")],
        version="2",
        boundary=True,
    )
    live = put_checkpoint(
        settings.data_plane.db_url, tid, [AIMessage(id="live-source", content="live")], version="3"
    )
    with gateway_test_client(machine=machine_identity) as client:
        _ = reconfigure(client, timeline_compact_history=1)
        for source_item_id, source_message_id in [
            (f"s2.{older}.0.0", "old-source"),
            (f"s1.{live}.0.0", "live-source"),
            ("s1.nonexistent-checkpoint.0.0", "missing"),
        ]:
            response = client.post(
                f"/api/agents/{tid}/messages",
                json={
                    "content": "Explain",
                    "source": "user",
                    "quote": {
                        "source_agent_id": tid,
                        "source_item_id": source_item_id,
                        "source_message_id": source_message_id,
                        "author": "Agent",
                        "text": "selection",
                    },
                },
            )
            assert response.status_code == 422, response.text
            assert "Retained quote source is no longer available" in response.json()["detail"]
    assert _pending_rows(db_conn, tid) == []
