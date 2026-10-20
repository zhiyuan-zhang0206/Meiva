from __future__ import annotations

import json
from collections.abc import Iterator

import psycopg
import pytest
from fastapi.testclient import TestClient

from base.events.notifications.store import SETTINGS_KEY
from gateway.tests.events.test_notices_endpoint import insert_notice, seed_agent
from tests.fixtures.gateway_config import gateway_test_client

_CLUSTER_AUTH = "notification-audit-test-value"
_PATH = "/api/notifications/audit"
_NEEDS_INPUT = "core.needs_input"


@pytest.fixture
def client() -> Iterator[TestClient]:
    with gateway_test_client(
        cluster_secret=_CLUSTER_AUTH, headers={"Authorization": f"Bearer {_CLUSTER_AUTH}"}
    ) as value:
        yield value


# meiva:note
# Three needs-input candidates inside a 7-day window, plus rows the audit must skip.
def _seed(db_conn: psycopg.Connection) -> None:
    agent = seed_agent(db_conn)
    plain = insert_notice(db_conn, agent, "plain", require_response=True)
    re_asked = insert_notice(db_conn, agent, "re-asked", require_response=True)
    old = insert_notice(db_conn, agent, "old", require_response=True)
    insert_notice(db_conn, agent, "fyi", require_response=False)
    db_conn.execute(
        "UPDATE agent_notices SET created_at = now() - interval '2 hours' WHERE id = %s", (plain,)
    )
    db_conn.execute(
        "UPDATE agent_notices SET created_at = now() - interval '3 hours', "
        "updated_at = now() - interval '170 minutes', question = %s WHERE id = %s",
        (json.dumps({"prompt": "ship it?"}), re_asked),
    )
    db_conn.execute(
        "UPDATE agent_notices SET created_at = now() - interval '30 days' WHERE id = %s", (old,)
    )
    db_conn.commit()


def test_the_audit_replays_the_notice_rows(client: TestClient, db_conn: psycopg.Connection) -> None:
    _seed(db_conn)

    resp = client.get(_PATH, params={"days": 7, "tz": "UTC"})

    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["window"]["days"] == 7
    assert report["window"]["timezone"] == "UTC"
    assert report["categories"] == [_NEEDS_INPUT]
    assert report["switches"] == {_NEEDS_INPUT: True}
    assert report["delivered"]["total"] == 3
    assert report["delivered"]["per_category"] == {_NEEDS_INPUT: 3}
    assert len(report["delivered"]["per_hour"]) == 24
    assert report["candidates"] == [
        {
            "category": _NEEDS_INPUT,
            "candidates": 3,
            "delivered": 3,
            "suppressed": 0,
            "repeat_deliveries": 0,
        }
    ]
    assert report["simulations"] == [
        {"variant": "baseline", "delivered": 3, "delta": 0, "per_day": round(3 / 7, 3)}
    ]
    assert report["explain"] is None


def test_a_switched_off_category_is_counted_as_suppressed(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    _seed(db_conn)
    db_conn.execute(
        "INSERT INTO user_settings (key, value) VALUES (%s, %s)",
        (SETTINGS_KEY, json.dumps({_NEEDS_INPUT: False})),
    )
    db_conn.commit()

    report = client.get(_PATH, params={"days": 7, "tz": "UTC", "explain": True}).json()

    assert report["switches"] == {_NEEDS_INPUT: False}
    assert report["delivered"]["total"] == 0
    assert report["candidates"][0]["suppressed"] == 3
    assert [row["outcome"] for row in report["explain"]] == ["off", "off", "off"]


def test_the_requestion_simulation_collapses_a_re_ask(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    _seed(db_conn)

    report = client.get(
        _PATH,
        params={"days": 7, "tz": "UTC", "category": _NEEDS_INPUT, "simulate": ["requestion=off"]},
    ).json()

    assert [row["variant"] for row in report["simulations"]] == ["baseline", "requestion=off"]
    assert report["simulations"][1]["delivered"] == 2
    assert report["simulations"][1]["delta"] == -1


@pytest.mark.parametrize(
    "params",
    [
        {"days": 0},
        {"tz": "Nowhere/Land"},
        {"category": "nobody.nothing"},
        {"simulate": ["requestion=maybe"]},
        {"now": "2026-10-01T12:00:00"},
    ],
)
def test_a_bad_input_is_rejected(
    client: TestClient, params: dict[str, str | int | list[str]]
) -> None:
    assert client.get(_PATH, params=params).status_code == 422


def test_the_audit_requires_authentication(client: TestClient) -> None:
    assert client.get(_PATH, headers={"Authorization": "Bearer revoked"}).status_code == 401
    assert client.get(_PATH, headers={"Authorization": ""}).status_code == 401
