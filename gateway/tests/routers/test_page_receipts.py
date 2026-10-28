"""Guarded registry acceptance survives lost responses without replacing later pages."""

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import psycopg
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from base.clock import SYSTEM_TIMERS
from base.clock.tests.seeded_rng import seeded_rng
from base.db import create_agent
from base.events.live.bus import EventBus, EventBusConfig
from gateway.http.config import PageSettings
from gateway.routers.page_serving import pages
from gateway.tests.auth.gateway_config import (
    GatewayTestClient,
    gateway_test_client,
    gateway_test_config,
)
from gateway.tests.support.db_faults.insert_fault import failing_insert

SECRET = "page-receipt-test-secret"  # noqa: S105 -- isolated credential fixture
HEADERS = {"Idempotency-Key": "intent", "Idempotency-Scope": "principal-v1"}
BODY = {"name": "report", "port": 8801, "host": "127.0.0.1", "ttl_seconds": 60}


@pytest.fixture
def client() -> Iterator[GatewayTestClient]:
    with gateway_test_client(
        fields={"cluster_secret": SECRET},
        headers={"Authorization": f"Bearer {SECRET}"},
    ) as value:
        yield value


# meiva:note
# A bus whose Redis URL names no scheme: any publish through it raises.
#
# A route that must not publish (a replay, an already-closed observation) is exercised
# against it and answers normally; one that publishes after its commit fails visibly.
def _unreachable_bus(client: GatewayTestClient) -> EventBus:
    return EventBus(
        EventBusConfig(redis_url="no-scheme", events_channel=client.app.state.bus.channel),
        rng=seeded_rng(),
        timers=SYSTEM_TIMERS,
    )


# meiva:note
# Block until some backend is waiting on a lock, so the test acts after the request is stuck.
def _wait_until_a_statement_waits_for_a_lock(db_url: str, *, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    with psycopg.connect(db_url, autocommit=True) as probe:
        while time.monotonic() < deadline:
            waiting = probe.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
            ).fetchone()
            if waiting is not None and waiting[0] > 0:
                return
            time.sleep(0.02)
    raise AssertionError("no statement started waiting for a lock")


def _agent(conn: psycopg.Connection) -> int:
    agent = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta (id, status) VALUES (%s, 'idling') ON CONFLICT DO NOTHING",
        (agent,),
    )
    conn.commit()
    return agent


def _path(agent: int) -> str:
    return f"/api/keyed/v1/agents/{agent}/pages"


def _close_path(agent: int) -> str:
    return _path(agent) + "/report/close"


def _key(value: str) -> dict[str, str]:
    return {**HEADERS, "Idempotency-Key": value}


@pytest.mark.parametrize("close", [False, True])
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Idempotency-Key": "x"},
        {"Idempotency-Key": "", "Idempotency-Scope": "principal-v1"},
        {"Idempotency-Key": "x" * 129, "Idempotency-Scope": "principal-v1"},
        {"Idempotency-Key": "x", "Idempotency-Scope": "other"},
    ],
)
def test_invalid_key_scope_has_no_effect(
    client: TestClient, db_conn: psycopg.Connection, headers: dict[str, str], close: bool
) -> None:
    agent = _agent(db_conn)
    body = {"expected_page_id": 1} if close else BODY
    path = _close_path(agent) if close else _path(agent)
    assert client.post(path, json=body, headers=headers).status_code == 422
    assert db_conn.execute("SELECT count(*) FROM page_operation_receipts").fetchone() == (0,)
    assert db_conn.execute("SELECT count(*) FROM agent_pages").fetchone() == (0,)


def test_lost_response_concurrency_replays_original_row(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = _agent(db_conn)
    first = client.post(_path(agent), json=BODY, headers=HEADERS)
    assert first.status_code == 201, first.text
    deadline = db_conn.execute(
        "SELECT expires_at FROM agent_pages WHERE id=%s", (first.json()["id"],)
    ).fetchone()
    db_conn.commit()
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(client.post, _path(agent), json=BODY, headers=HEADERS) for _ in range(4)
        ]
        responses = [future.result() for future in futures]
    assert all(
        response.status_code == 201 and response.json() == first.json() for response in responses
    )
    assert db_conn.execute("SELECT count(*) FROM agent_pages").fetchone() == (1,)
    assert (
        db_conn.execute(
            "SELECT expires_at FROM agent_pages WHERE id=%s", (first.json()["id"],)
        ).fetchone()
        == deadline
    )
    assert db_conn.execute("SELECT count(*) FROM page_operation_receipts").fetchone() == (1,)


def test_registration_replay_cannot_close_later_page(
    client: GatewayTestClient, db_conn: psycopg.Connection
) -> None:
    agent = _agent(db_conn)
    first = client.post(_path(agent), json=BODY, headers=HEADERS)
    later = client.post(_path(agent), json={**BODY, "port": 8802}, headers=_key("new"))
    assert later.status_code == 201 and later.json()["id"] != first.json()["id"]
    changed = replace(
        client.app.state.gateway_config,
        pages=PageSettings(gateway_url="http://changed.invalid", default_ttl_seconds=1),
    )

    # meiva:note
    # Replaying a historical acceptance must not publish obsolete events: a publish raises. The
    # replay runs on an app whose page settings differ from the accepting one's, and still
    # answers what was accepted.
    with gateway_test_client(
        process=client.app.state.gateway_process,
        config=changed,
        headers={"Authorization": f"Bearer {SECRET}"},
    ) as replaying:
        replaying.app.state.bus = _unreachable_bus(replaying)
        replay = replaying.post(_path(agent), json=BODY, headers=HEADERS)
    assert replay.status_code == 201 and replay.json() == first.json()
    assert db_conn.execute("SELECT id FROM agent_pages WHERE closed_at IS NULL").fetchall() == [
        (later.json()["id"],)
    ]


@pytest.mark.parametrize("mutate", ["terminate", "delete"])
def test_historical_acceptance_precedes_mutable_target(
    client: TestClient, db_conn: psycopg.Connection, mutate: str
) -> None:
    agent = _agent(db_conn)
    first = client.post(_path(agent), json=BODY, headers=HEADERS)
    if mutate == "delete":
        db_conn.execute("DELETE FROM agents_meta WHERE id=%s", (agent,))
        db_conn.execute("DELETE FROM agent_lifecycle_intervals WHERE agent_id=%s", (agent,))
        db_conn.execute("DELETE FROM agents WHERE id=%s", (agent,))
    else:
        db_conn.execute(
            "UPDATE agents_meta SET status='terminated',termination_source='user' WHERE id=%s",
            (agent,),
        )
    db_conn.commit()
    replay = client.post(_path(agent), json=BODY, headers=HEADERS)
    assert replay.status_code == 201 and replay.json() == first.json()
    assert client.post(_path(agent), json=BODY, headers=_key("fresh")).status_code == (
        404 if mutate == "delete" else 409
    )


def test_changed_body_conflicts_without_replacement(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = _agent(db_conn)
    first = client.post(_path(agent), json=BODY, headers=HEADERS)
    assert (
        client.post(_path(agent), json={**BODY, "title": "changed"}, headers=HEADERS).status_code
        == 409
    )
    assert db_conn.execute("SELECT id FROM agent_pages WHERE closed_at IS NULL").fetchall() == [
        (first.json()["id"],)
    ]


def test_close_receipt_and_stale_observation_preserve_new_page(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = _agent(db_conn)
    first = client.post(_path(agent), json=BODY, headers=HEADERS).json()
    close_body = {"expected_page_id": first["id"]}
    accepted = client.post(_close_path(agent), json=close_body, headers=_key("close"))
    assert accepted.status_code == 200
    later = client.post(_path(agent), json=BODY, headers=_key("later")).json()
    replay = client.post(_close_path(agent), json=close_body, headers=_key("close"))
    assert replay.json() == accepted.json() and replay.status_code == 200
    assert (
        client.post(_close_path(agent), json=close_body, headers=_key("stale")).status_code == 409
    )
    assert (
        client.post(
            _close_path(agent), json={"expected_page_id": later["id"]}, headers=_key("close")
        ).status_code
        == 409
    )
    assert db_conn.execute("SELECT id FROM agent_pages WHERE closed_at IS NULL").fetchall() == [
        (later["id"],)
    ]


def test_fresh_already_closed_observation_is_accepted(
    client: GatewayTestClient, db_conn: psycopg.Connection
) -> None:
    agent = _agent(db_conn)
    first = client.post(_path(agent), json=BODY, headers=HEADERS).json()
    body = {"expected_page_id": first["id"]}
    accepted = client.post(_close_path(agent), json=body, headers=_key("close"))

    # meiva:note
    # An already-closed acceptance must not publish obsolete name hints: a publish raises.
    client.app.state.bus = _unreachable_bus(client)
    no_effect = client.post(_close_path(agent), json=body, headers=_key("already-closed"))
    assert no_effect.status_code == 200 and no_effect.json() == accepted.json()


@pytest.mark.parametrize("value", [0, -1, True, "1"])
def test_observed_id_is_strict(
    client: TestClient, db_conn: psycopg.Connection, value: object
) -> None:
    agent = _agent(db_conn)
    assert (
        client.post(
            _close_path(agent), json={"expected_page_id": value}, headers=HEADERS
        ).status_code
        == 422
    )
    assert db_conn.execute("SELECT count(*) FROM page_operation_receipts").fetchone() == (0,)


def test_failure_before_receipt_rolls_back_replacement(
    client: GatewayTestClient, db_conn: psycopg.Connection
) -> None:
    agent = _agent(db_conn)
    first = client.post(_path(agent), json=BODY, headers=HEADERS)
    with (
        failing_insert(
            db_conn, "page_operation_receipts", message="fault before page receipt commit"
        ),
        pytest.raises(psycopg.errors.RaiseException),
    ):
        client.post(_path(agent), json={**BODY, "port": 8802}, headers=_key("pending"))
    assert db_conn.execute("SELECT id FROM agent_pages WHERE closed_at IS NULL").fetchall() == [
        (first.json()["id"],)
    ]
    assert db_conn.execute("SELECT count(*) FROM page_operation_receipts").fetchone() == (1,)
    db_conn.commit()
    recovered = client.post(_path(agent), json={**BODY, "port": 8802}, headers=_key("pending"))
    assert recovered.status_code == 201


def test_revoked_auth_cannot_replay(client: GatewayTestClient, db_conn: psycopg.Connection) -> None:
    agent = _agent(db_conn)
    assert client.post(_path(agent), json=BODY, headers=HEADERS).status_code == 201
    with gateway_test_client(
        process=client.app.state.gateway_process,
        config=replace(client.app.state.gateway_config, cluster_secret="different-secret"),  # noqa: S106 -- isolated test credential
        headers={"Authorization": f"Bearer {SECRET}"},
    ) as rotated:
        assert rotated.post(_path(agent), json=BODY, headers=HEADERS).status_code == 401


def test_older_routing_has_no_guarded_effect(db_conn: psycopg.Connection) -> None:
    old = FastAPI()
    legacy = APIRouter()
    legacy.routes.extend(
        route
        for route in pages.router.routes
        if isinstance(route, APIRoute) and "/api/keyed/v1/" not in route.path
    )
    old.include_router(legacy)
    agent = _agent(db_conn)
    with TestClient(old) as client:
        assert client.post(_path(agent), json=BODY, headers=HEADERS).status_code == 404
        assert (
            client.post(
                _close_path(agent), json={"expected_page_id": 1}, headers=HEADERS
            ).status_code
            == 404
        )
    assert db_conn.execute("SELECT count(*) FROM agent_pages").fetchone() == (0,)


def test_simultaneous_fresh_duplicates_publish_one_row(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = _agent(db_conn)
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(client.post, _path(agent), json=BODY, headers=HEADERS) for _ in range(4)
        ]
        results = [future.result() for future in futures]
    assert all(
        result.status_code == 201 and result.json() == results[0].json() for result in results
    )
    assert db_conn.execute("SELECT count(*) FROM agent_pages").fetchone() == (1,)
    assert db_conn.execute("SELECT count(*) FROM page_operation_receipts").fetchone() == (1,)


def test_two_fresh_intents_serialize_replacement(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = _agent(db_conn)
    with ThreadPoolExecutor(max_workers=2) as executor:
        one = executor.submit(client.post, _path(agent), json=BODY, headers=HEADERS)
        two = executor.submit(
            client.post,
            _path(agent),
            json={**BODY, "name": "later", "port": 8802},
            headers=_key("new"),
        )
        results = [one.result(), two.result()]
    assert all(result.status_code == 201 for result in results)
    assert len({result.json()["id"] for result in results}) == 2
    assert db_conn.execute(
        "SELECT count(*) FROM agent_pages WHERE closed_at IS NULL"
    ).fetchone() == (1,)
    assert db_conn.execute("SELECT count(*) FROM page_operation_receipts").fetchone() == (2,)


def test_commit_survives_response_tail_failure(
    client: GatewayTestClient, db_conn: psycopg.Connection
) -> None:
    agent = _agent(db_conn)
    working_bus = client.app.state.bus
    client.app.state.bus = _unreachable_bus(client)
    with pytest.raises(ValueError, match="Redis URL"):
        client.post(_path(agent), json=BODY, headers=HEADERS)
    client.app.state.bus = working_bus
    row = db_conn.execute("SELECT id FROM agent_pages WHERE agent_id=%s", (agent,)).fetchone()
    replay = client.post(_path(agent), json=BODY, headers=HEADERS)
    assert row is not None and replay.status_code == 201 and replay.json()["id"] == row[0]
    assert db_conn.execute("SELECT count(*) FROM agent_pages").fetchone() == (1,)


def test_raced_unique_conflict_rolls_back_old_close(
    client: GatewayTestClient, db_conn: psycopg.Connection, db_url: str
) -> None:
    one, two, three = _agent(db_conn), _agent(db_conn), _agent(db_conn)
    original = client.post(_path(one), json=BODY, headers=HEADERS).json()
    assert client.post(_path(two), json={**BODY, "port": 8802}, headers=HEADERS).status_code == 201
    # meiva:note
    # Close two's page: the advisory pre-check now sees (host, 8802) free.
    two_page = db_conn.execute("SELECT id FROM agent_pages WHERE agent_id=%s", (two,)).fetchone()
    assert two_page is not None
    db_conn.execute("UPDATE agent_pages SET closed_at = now() WHERE id=%s", (two_page[0],))
    db_conn.commit()
    # meiva:note
    # A third agent's registration of the same port is still uncommitted: invisible to the
    # pre-check, but it holds the live-port unique index entry the insert will need.
    db_conn.execute(
        "INSERT INTO agent_pages (agent_id, name, host, port) VALUES (%s, 'raced', '127.0.0.1', 8802)",
        (three,),
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        racing = executor.submit(
            client.post, _path(one), json={**BODY, "port": 8802}, headers=_key("raced")
        )
        _wait_until_a_statement_waits_for_a_lock(db_url)
        # meiva:note
        # the other registration wins: the insert now violates the index
        db_conn.commit()
        rejected = racing.result(timeout=10)
    assert rejected.status_code == 409
    assert db_conn.execute(
        "SELECT id FROM agent_pages WHERE agent_id=%s AND closed_at IS NULL", (one,)
    ).fetchone() == (original["id"],)
    assert db_conn.execute("SELECT count(*) FROM page_operation_receipts").fetchone() == (2,)


def test_close_replay_survives_target_cleanup(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = _agent(db_conn)
    record = client.post(_path(agent), json=BODY, headers=HEADERS).json()
    body = {"expected_page_id": record["id"]}
    closed = client.post(_close_path(agent), json=body, headers=HEADERS)
    db_conn.execute("DELETE FROM agents_meta WHERE id=%s", (agent,))
    db_conn.execute("DELETE FROM agent_lifecycle_intervals WHERE agent_id=%s", (agent,))
    db_conn.execute("DELETE FROM agents WHERE id=%s", (agent,))
    db_conn.commit()
    replay = client.post(_close_path(agent), json=body, headers=HEADERS)
    assert replay.status_code == 200 and replay.json() == closed.json()
    assert client.post(_close_path(agent), json=body, headers=_key("fresh")).status_code == 404


def test_host_validation_reuses_single_borrowed_connection(db_conn: psycopg.Connection) -> None:
    agent = _agent(db_conn)
    db_conn.execute("UPDATE agents_meta SET machine='page-home' WHERE id=%s", (agent,))
    db_conn.commit()
    # A guarded receipt lookup already owns the only backend. Validation must
    # share it rather than exhaust a pool with a nested connection request.
    with gateway_test_client(
        config=gateway_test_config(db_pool_max_size=1, cluster_secret=SECRET),
        headers={"Authorization": f"Bearer {SECRET}"},
    ) as one_connection:
        result = one_connection.post(
            _path(agent), json={**BODY, "host": "page-home"}, headers=HEADERS
        )
    assert result.status_code == 201, result.text


def test_placement_is_locked_before_host_validation(
    client: GatewayTestClient, db_conn: psycopg.Connection, db_url: str
) -> None:
    agent = _agent(db_conn)
    db_conn.execute("UPDATE agents_meta SET machine='page-home' WHERE id=%s", (agent,))
    db_conn.commit()
    # meiva:note
    # Host validation reads the machine's advertised addresses; holding that table
    # stalls the request after it has locked the placement, so the lock can be probed.
    db_conn.execute("LOCK TABLE machine_units IN ACCESS EXCLUSIVE MODE")
    with ThreadPoolExecutor(max_workers=1) as executor:
        accepted = executor.submit(
            client.post, _path(agent), json={**BODY, "host": "page-home"}, headers=HEADERS
        )
        try:
            _wait_until_a_statement_waits_for_a_lock(db_url)
            with psycopg.connect(db_url) as other:
                other.execute("SET LOCAL lock_timeout='100ms'")
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    other.execute(
                        "UPDATE agents_meta SET machine='moved-home' WHERE id=%s", (agent,)
                    )
                other.rollback()
        finally:
            db_conn.commit()
        assert accepted.result(timeout=10).status_code == 201
