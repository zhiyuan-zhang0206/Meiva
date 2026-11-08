from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from base.paths import meiva_home
from base.telemetry.observability import cluster_label
from gateway.tests.auth.gateway_config import (
    GatewayTestClient,
    gateway_test_client,
    gateway_test_config,
)


@pytest.fixture
def client_config() -> dict[str, Any]:
    """`GatewayConfig` fields the test client is built with; a test overrides them."""
    return {}


@pytest.fixture
def client(db_conn: psycopg.Connection, client_config: dict[str, Any]) -> Any:
    with db_conn.cursor() as cur:
        cur.execute("TRUNCATE event_dismissals RESTART IDENTITY")
    db_conn.commit()
    with gateway_test_client(config=gateway_test_config(**client_config)) as test_client:
        yield test_client


def _event(
    db: psycopg.Connection,
    event_name: str,
    *,
    level: str = "warning",
    source: str = "svc",
    process: str = "gateway",
    category: str = "telemetry",
    minutes_ago: float = 1,
    cluster: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> None:
    db.execute(
        "INSERT INTO telemetry_events (event_uid, ts, machine, cluster, process, category, "
        "event_name, level, source, attributes) "
        "VALUES (%s, %s, 'm1', %s, %s, %s, %s, %s, %s, %s::jsonb)",
        (
            uuid.uuid4().int % (1 << 62),
            datetime.now(UTC) - timedelta(minutes=minutes_ago),
            cluster_label(meiva_home()) if cluster is None else cluster,
            process,
            category,
            event_name,
            level,
            source,
            json.dumps(attributes or {}),
        ),
    )


def _dismiss(
    db: psycopg.Connection, event_name: str, *, level: str = "warning", process: str = ""
) -> int:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO event_dismissals (category, level, event_name, source, process, "
            "dismissed_by) VALUES ('log', %s, %s, 'svc', %s, 0) RETURNING id",
            (level, event_name, process),
        )
        row = cur.fetchone()
    assert row is not None
    return row[0]


def test_classes_are_grouped_with_counts_times_and_most_frequent_first(
    db_conn: psycopg.Connection, client: TestClient
) -> None:
    for minutes_ago in (50, 20, 5):
        _event(db_conn, "disk_pressure", minutes_ago=minutes_ago)
    _event(db_conn, "disk_pressure", process="agent-host")
    _event(db_conn, "turn_failed", level="error", source="runner")
    _event(db_conn, "turn_failed", level="error", source="runner")
    _event(db_conn, "ignored_info", level="info")
    _event(db_conn, "elsewhere", cluster="another-cluster")
    _event(db_conn, "too_old", minutes_ago=60 * 30)
    db_conn.commit()

    body = client.get("/api/stats/alert-classes").json()

    assert body["window_hours"] == 24
    assert body["total_classes"] == 3
    assert body["total_events"] == 6
    rows = body["classes"]
    assert [(r["event_name"], r["process"], r["count"]) for r in rows] == [
        ("disk_pressure", "gateway", 3),
        ("turn_failed", "gateway", 2),
        ("disk_pressure", "agent-host", 1),
    ]
    first = rows[0]
    assert (first["level"], first["source"], first["category"]) == ("warning", "svc", "telemetry")
    assert first["dismissal_id"] is None
    assert datetime.fromisoformat(first["last_seen"]) - datetime.fromisoformat(
        first["first_seen"]
    ) == pytest.approx(timedelta(minutes=45), abs=timedelta(seconds=1))


# meiva:note
# Two horizons answer different questions: the window says how far back the list looks, the quiet
# rule says which of those classes still needs attention. A class three hours old is inside the
# 24-hour window and still resolved, so the default list is the intersection.
# meiva:link gateway/cluster/docs/ops-surfaces.meiva.okf.md
def test_the_window_selects_which_events_form_classes(
    db_conn: psycopg.Connection, client: TestClient
) -> None:
    _event(db_conn, "recent", minutes_ago=3)
    _event(db_conn, "hours_old", minutes_ago=180)
    db_conn.commit()

    last_hour = client.get("/api/stats/alert-classes", params={"hours": 1}).json()
    last_day = client.get("/api/stats/alert-classes", params={"hours": 24}).json()
    history = client.get(
        "/api/stats/alert-classes", params={"hours": 24, "include_resolved": True}
    ).json()

    assert [r["event_name"] for r in last_hour["classes"]] == ["recent"]
    assert [r["event_name"] for r in last_day["classes"]] == ["recent"]
    assert {r["event_name"]: r["resolved_by"] for r in history["classes"]} == {
        "recent": None,
        "hours_old": "quiet",
    }
    assert client.get("/api/stats/alert-classes", params={"hours": 5}).status_code == 422


def test_dismissed_classes_carry_the_cancelling_dismissal(
    db_conn: psycopg.Connection, client: TestClient
) -> None:
    wildcard = _dismiss(db_conn, "wild")
    exact = _dismiss(db_conn, "scoped", process="agent-host")
    _event(db_conn, "wild", process="gateway")
    _event(db_conn, "wild", process="agent-host", category="log")
    _event(db_conn, "scoped", process="agent-host")
    _event(db_conn, "scoped", process="gateway")
    reopened = _dismiss(db_conn, "reopened")
    db_conn.execute("UPDATE event_dismissals SET status = 'reopened' WHERE id = %s", (reopened,))
    _event(db_conn, "reopened")
    db_conn.commit()

    rows = client.get("/api/stats/alert-classes", params={"include_resolved": True}).json()[
        "classes"
    ]

    by_identity = {(r["event_name"], r["process"]): r["dismissal_id"] for r in rows}
    assert by_identity == {
        ("wild", "gateway"): wildcard,
        ("wild", "agent-host"): wildcard,
        ("scoped", "agent-host"): exact,
        ("scoped", "gateway"): None,
        ("reopened", "gateway"): None,
    }


# meiva:note
# A dismissed class leaves the default list and both unresolved totals; the resolved pair keeps
# the events accounted for, so the number that fell is still explained.
# meiva:link docs/decisions/observability/events/2026-11-08-quiet-auto-resolution.md
def test_dismissing_through_the_api_moves_the_class_out_of_the_active_count(
    db_conn: psycopg.Connection, client: TestClient
) -> None:
    _event(db_conn, "noisy", process="gateway", category="log", minutes_ago=2)
    _event(db_conn, "noisy", process="gateway", category="log", minutes_ago=1)
    db_conn.commit()
    row = client.get("/api/stats/alert-classes").json()["classes"][0]
    assert client.get("/api/stats/dashboard").json()["alert_classes_active"] == 1

    created = client.post(
        "/api/event-resolutions",
        json={key: row[key] for key in ("category", "level", "event_name", "source", "process")},
    )
    assert created.status_code == 201

    resolved = client.get("/api/stats/alert-classes", params={"include_resolved": True}).json()[
        "classes"
    ][0]
    assert (resolved["dismissal_id"], resolved["resolved_by"]) == (
        created.json()["id"],
        "dismissed",
    )
    assert client.get("/api/stats/alert-classes").json()["classes"] == []
    dashboard = client.get("/api/stats/dashboard").json()
    assert (dashboard["alert_classes_active"], dashboard["alert_classes_dismissed"]) == (0, 1)
    assert (dashboard["warnings"], dashboard["warnings_resolved"]) == (0, 2)

    reopened = client.post(f"/api/event-resolutions/{resolved['dismissal_id']}/reopen")
    assert reopened.status_code == 200
    assert client.get("/api/stats/alert-classes").json()["classes"][0]["dismissal_id"] is None
    back = client.get("/api/stats/dashboard").json()
    assert (back["alert_classes_active"], back["warnings"], back["warnings_resolved"]) == (1, 2, 0)


@pytest.mark.parametrize("client_config", [{"alert_classes_limit": 2}])
def test_the_class_list_is_capped_but_reports_its_full_size(
    db_conn: psycopg.Connection, client: GatewayTestClient
) -> None:
    for name, repeats in (("a", 3), ("b", 2), ("c", 1)):
        for _ in range(repeats):
            _event(db_conn, name)
    db_conn.commit()

    body = client.get("/api/stats/alert-classes").json()

    assert [r["event_name"] for r in body["classes"]] == ["a", "b"]
    assert (body["total_classes"], body["total_events"]) == (3, 6)


@pytest.mark.parametrize("client_config", [{"alert_samples_limit": 2}])
def test_samples_are_the_newest_events_of_exactly_one_class(
    db_conn: psycopg.Connection, client: GatewayTestClient
) -> None:
    _event(db_conn, "boom", minutes_ago=30, attributes={"msg": "oldest"})
    _event(db_conn, "boom", minutes_ago=20, attributes={"msg": "middle", "detail": 7})
    _event(db_conn, "boom", minutes_ago=10, attributes={"msg": "newest"})
    _event(db_conn, "boom", process="agent-host", attributes={"msg": "other process"})
    _event(db_conn, "boom", level="error", attributes={"msg": "other level"})
    _event(db_conn, "quiet", attributes={"msg": "other class"})
    db_conn.commit()

    samples = client.get(
        "/api/stats/alert-classes/samples",
        params={"level": "warning", "event_name": "boom", "source": "svc", "process": "gateway"},
    ).json()["samples"]

    assert [s["message"] for s in samples] == ["newest", "middle"]
    assert samples[0]["machine"] == "m1"
    assert samples[1]["attributes"] == {"msg": "middle", "detail": 7}
    assert samples[0]["agent_id"] is None


# meiva:note
# The positive/negative pair the user ruling asks for: a pulse that stopped counts as resolved and
# leaves both totals; a class that is still recording stays unresolved and keeps them. Same window,
# same query — only `last_seen` differs.
# meiva:link docs/decisions/observability/events/2026-11-08-quiet-auto-resolution.md
def test_a_stopped_pulse_is_resolved_and_a_live_class_is_not(
    db_conn: psycopg.Connection, client: TestClient
) -> None:
    _event(db_conn, "stopped_pulse", minutes_ago=180)
    _event(db_conn, "still_happening", minutes_ago=1)
    _event(db_conn, "still_happening", minutes_ago=2)
    db_conn.commit()

    listed = client.get("/api/stats/alert-classes").json()
    assert [(r["event_name"], r["resolved_by"]) for r in listed["classes"]] == [
        ("still_happening", None)
    ]
    dashboard = client.get("/api/stats/dashboard").json()
    assert (dashboard["warnings"], dashboard["warnings_resolved"]) == (2, 1)
    assert (dashboard["alert_classes_active"], dashboard["alert_classes_dismissed"]) == (1, 1)


# meiva:note
# A class that starts again after a quiet stretch is unresolved on the next read — the quiet rule
# is derived from `last_seen`, so recurrence reopens it without a stored row to reopen.
# meiva:link docs/decisions/observability/events/2026-11-08-quiet-auto-resolution.md
def test_a_class_that_starts_again_after_a_quiet_stretch_is_unresolved(
    db_conn: psycopg.Connection, client: TestClient
) -> None:
    _event(db_conn, "flapping", minutes_ago=180)
    db_conn.commit()
    assert client.get("/api/stats/alert-classes").json()["classes"] == []

    _event(db_conn, "flapping", minutes_ago=0)
    db_conn.commit()
    listed = client.get("/api/stats/alert-classes").json()["classes"]
    assert [(r["event_name"], r["resolved_by"]) for r in listed] == [("flapping", None)]


# meiva:note
# The window is a knob, not a literal: with a one-minute window a class silent for three minutes
# is resolved, and the same rows read as unresolved under the default hour.
# meiva:link docs/decisions/observability/events/2026-11-08-quiet-auto-resolution.md
@pytest.mark.parametrize("client_config", [{"alert_class_quiet_seconds": 60.0}])
def test_the_quiet_window_is_the_configured_one(
    db_conn: psycopg.Connection, client: GatewayTestClient
) -> None:
    _event(db_conn, "three_minutes_ago", minutes_ago=3)
    db_conn.commit()

    assert client.get("/api/stats/alert-classes").json()["classes"] == []
    assert client.get("/api/stats/dashboard").json()["warnings_resolved"] == 1


def test_samples_without_a_message_attribute_have_a_null_message(
    db_conn: psycopg.Connection, client: TestClient
) -> None:
    _event(db_conn, "structured", process="", attributes={"reason": "x"})
    db_conn.commit()

    samples = client.get(
        "/api/stats/alert-classes/samples",
        params={"level": "warning", "event_name": "structured", "source": "svc"},
    ).json()["samples"]

    assert len(samples) == 1
    assert samples[0]["message"] is None
    assert samples[0]["attributes"] == {"reason": "x"}


def test_samples_reject_an_unknown_level(client: TestClient) -> None:
    response = client.get(
        "/api/stats/alert-classes/samples",
        params={"level": "info", "event_name": "x", "source": "svc"},
    )
    assert response.status_code == 422
