from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from base.db.rows import IdRow, query_one
from base.events.live.bus import EventBus
from gateway.events.sse import event_stream
from gateway.events.sse_frames import SseCadence
from gateway.tests.auth.gateway_config import GatewayTestClient, gateway_test_client
from gateway.tests.events.sse_support import FakeRequest, refused_redis_url
from gateway.tests.routers.alerts_api.alerts_support import (
    alert,
    alerts_client,
    ingest,
    seed_alert_rows,
    webhook,
)
from gateway.tests.routers.alerts_api.alerts_support import (
    client as client,
)
from ops.alerts import ALERTS_CHANNEL, AlertRow


def test_list_unresolved_first_and_filters(db_conn: psycopg.Connection, client: TestClient) -> None:
    now = datetime.now(UTC)
    seed_alert_rows(
        db_conn,
        [
            {
                "status": "unresolved",
                "severity": "error",
                "alertname": "r1",
                "starts_at": now - timedelta(hours=1),
                "ends_at": None,
                "fingerprint": "f1",
            },
            {
                "status": "resolved",
                "severity": "warning",
                "alertname": "r2",
                "starts_at": now - timedelta(hours=2),
                "ends_at": now - timedelta(hours=1, minutes=50),
                "fingerprint": "f2",
            },
            {
                "status": "unresolved",
                "severity": "error",
                "alertname": "r3",
                "starts_at": now - timedelta(minutes=5),
                "ends_at": None,
                "fingerprint": "f3",
            },
        ],
    )

    resp = client.get("/api/alerts")
    assert resp.status_code == 200
    body = resp.json()
    assert [a["fingerprint"] for a in body["alerts"]] == ["f3", "f1", "f2"]
    assert set(body["meta"]) == {"window", "total", "unresolved_count"}
    assert body["meta"]["unresolved_count"] == 2
    assert body["meta"]["total"] == 3

    resp = client.get("/api/alerts?severity=warning")
    assert [a["fingerprint"] for a in resp.json()["alerts"]] == ["f2"]

    resp = client.get("/api/alerts?status=unresolved")
    assert [a["fingerprint"] for a in resp.json()["alerts"]] == ["f3", "f1"]

    resp = client.get("/api/alerts?status=resolved")
    body = resp.json()
    assert [a["fingerprint"] for a in body["alerts"]] == ["f2"]
    assert body["meta"]["unresolved_count"] == 0

    resp = client.get("/api/alerts?window=1h")
    assert [a["fingerprint"] for a in resp.json()["alerts"]] == ["f3", "f1"]

    resp = client.get("/api/alerts?limit=1")
    assert [a["fingerprint"] for a in resp.json()["alerts"]] == ["f3"]
    assert resp.json()["meta"]["total"] == 3


@pytest.mark.parametrize("window", ["1h", "6h", "24h", "7d"])
def test_live_alerts_never_age_out_and_history_uses_recovery_time(
    db_conn: psycopg.Connection, client: TestClient, window: str
) -> None:
    now = datetime.now(UTC)
    seed_alert_rows(
        db_conn,
        [
            {
                "status": status,
                "severity": severity,
                "alertname": fingerprint,
                "starts_at": now - timedelta(days=10),
                "ends_at": end,
                "fingerprint": fingerprint,
            }
            for status, severity, end, fingerprint in [
                ("unresolved", "critical", None, "old-live"),
                ("unresolved", "warning", None, "old-warning"),
                ("resolved", "critical", now - timedelta(minutes=10), "recent-recovery"),
                ("resolved", "critical", now - timedelta(days=9), "old-recovery"),
            ]
        ],
    )
    result = client.get(f"/api/alerts?window={window}").json()
    assert {a["fingerprint"] for a in result["alerts"]} == {
        "old-live",
        "old-warning",
        "recent-recovery",
    }
    assert result["meta"]["unresolved_count"] == 2
    assert result["meta"]["total"] == 3
    limited = client.get(f"/api/alerts?window={window}&limit=1&severity=critical").json()
    assert limited["meta"]["unresolved_count"] == 1
    assert limited["meta"]["total"] == 2
    assert len(limited["alerts"]) == 1
    resolved = client.get(f"/api/alerts?window={window}&status=resolved").json()
    assert [a["fingerprint"] for a in resolved["alerts"]] == ["recent-recovery"]
    assert resolved["meta"]["unresolved_count"] == 0


def test_list_default_limit_comes_from_display_config(db_conn: psycopg.Connection) -> None:
    now = datetime.now(UTC)
    seed_alert_rows(
        db_conn,
        [
            {
                "status": "unresolved",
                "severity": "error",
                "alertname": f"r{i}",
                "starts_at": now - timedelta(minutes=i),
                "ends_at": None,
                "fingerprint": f"f{i}",
            }
            for i in range(3)
        ],
    )
    with gateway_test_client(fields={"alerts_default_limit": 2}) as limited_client:
        resp = limited_client.get("/api/alerts")
    assert resp.status_code == 200
    assert len(resp.json()["alerts"]) == 2


def test_list_unresolved_before_resolved(db_conn: psycopg.Connection, client: TestClient) -> None:
    now = datetime.now(UTC)
    seed_alert_rows(
        db_conn,
        [
            {
                "status": "resolved",
                "severity": "error",
                "alertname": "res-new",
                "starts_at": now - timedelta(minutes=5),
                "ends_at": now,
                "fingerprint": "f1",
            },
            {
                "status": "unresolved",
                "severity": "error",
                "alertname": "fire-old",
                "starts_at": now - timedelta(hours=1),
                "ends_at": None,
                "fingerprint": "f2",
            },
        ],
    )

    resp = client.get("/api/alerts")
    assert resp.status_code == 200
    body = resp.json()
    assert [a["fingerprint"] for a in body["alerts"]] == ["f2", "f1"]


def test_ingest_rows_are_grafana_sourced(db_conn: psycopg.Connection) -> None:
    with alerts_client() as client:
        ingest(client, webhook(alerts=[alert(fingerprint="s1")]))
        ingest(client, webhook(alerts=[alert(fingerprint="s1")]))
    with db_conn.cursor() as cur:
        cur.execute("SELECT source FROM alerts")
        assert cur.fetchall() == [("grafana",)]


def test_ingest_resolved_updates_the_same_row(db_conn: psycopg.Connection) -> None:
    with alerts_client() as client:
        ingest(client, webhook(alerts=[alert(fingerprint="hp2")]))
        ingest(
            client,
            webhook(
                status="resolved",
                alerts=[
                    alert(status="resolved", fingerprint="hp2", ends_at="2026-08-04T11:00:00Z")
                ],
            ),
        )
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM alerts")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 1


def test_list_returns_source(db_conn: psycopg.Connection, client: TestClient) -> None:
    now = datetime.now(UTC)
    seed_alert_rows(
        db_conn,
        [
            {
                "status": "unresolved",
                "severity": "error",
                "alertname": "m",
                "starts_at": now - timedelta(minutes=1),
                "ends_at": None,
                "fingerprint": "fm",
                "source": "grafana",
            }
        ],
    )
    body = client.get("/api/alerts").json()
    assert body["alerts"][0]["source"] == "grafana"


# meiva:note
# The route answers 200 with the SSE headers; a Redis that refuses the connection ends the
# stream with an error frame (the stream's own behavior is covered in `gateway/tests/events`).
def test_stream_endpoint_is_sse(db_conn: psycopg.Connection) -> None:
    with gateway_test_client(
        fields={"redis_url": refused_redis_url()}, raise_server_exceptions=False
    ) as client:
        resp = client.get("/api/alerts/stream")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["cache-control"] == "no-cache"
    assert resp.headers["x-accel-buffering"] == "no"
    assert b"event stream interrupted: ConnectionError" in resp.content


# meiva:note
# The stream the route serves is the alerts channel, broadcast, validated as an alert row.
def test_an_ingested_alert_reaches_a_subscriber_of_the_alerts_channel(
    db_conn: psycopg.Connection, event_bus: EventBus
) -> None:

    async def scenario(client: GatewayTestClient) -> list[bytes]:
        gen = event_stream(
            event_bus,
            0,
            # meiva:note
            # FakeRequest stands in for the ASGI request: the stream only asks it whether the client left.
            FakeRequest(),  # pyright: ignore[reportArgumentType]
            channel=ALERTS_CHANNEL,
            broadcast=True,
            validator=TypeAdapter(AlertRow),
            poll_s=0.25,
            cadence=SseCadence(),
        )
        frames = [await anext(gen)]
        await asyncio.to_thread(ingest, client, webhook(alerts=[alert(fingerprint="live")]))
        while not frames[-1].startswith(b"data: "):
            frames.append(await asyncio.wait_for(anext(gen), timeout=10.0))
        await gen.aclose()
        return frames

    with alerts_client() as client:
        frame = asyncio.run(scenario(client))[-1]
    assert b'"alertname":"test-rule"' in frame and b'"fingerprint":"live"' in frame


def test_watchdog_synthetic_heartbeat_excluded_from_list_and_counts(
    db_conn: psycopg.Connection, client: TestClient
) -> None:
    now = datetime.now(UTC)
    seed_alert_rows(
        db_conn,
        [
            {
                "status": "unresolved",
                "severity": "warning",
                "alertname": "Alert pipeline watchdog (synthetic heartbeat)",
                "labels": {"metric": "alert_pipeline_watchdog", "team": "meiva-ops"},
                "starts_at": now - timedelta(days=3),
                "ends_at": None,
                "fingerprint": "watchdog-1",
            },
            {
                "status": "unresolved",
                "severity": "error",
                "alertname": "Real alert",
                "labels": {"metric": "real_metric", "team": "meiva-ops"},
                "starts_at": now - timedelta(minutes=10),
                "ends_at": None,
                "fingerprint": "real-1",
            },
        ],
    )

    resp = client.get("/api/alerts")
    assert resp.status_code == 200
    body = resp.json()
    assert [a["fingerprint"] for a in body["alerts"]] == ["real-1"]
    assert body["meta"]["unresolved_count"] == 1
    assert body["meta"]["total"] == 1


def test_stopped_schedule_alerts_resolved_on_ingest(
    db_conn: psycopg.Connection, client: TestClient
) -> None:
    sched_row = query_one(
        db_conn,
        IdRow,
        "INSERT INTO schedules (name, script, command, enabled, status, machine) "
        "VALUES ('test-stopped', 'pass', 'python schedule.py', false, 'stopped', 'box') "
        "RETURNING id",
    )
    assert sched_row is not None
    sid = sched_row.id
    db_conn.commit()

    sched_alert = alert(
        alertname="Schedule lost its session",
        fingerprint="sched-loss-1",
    )
    sched_alert["labels"]["attributes_schedule_id"] = str(sid)
    sched_alert["labels"]["metric"] = "schedule_process_lost"
    ingest(client, webhook(alerts=[sched_alert]))

    alert_row = query_one(
        db_conn,
        AlertRow,
        "SELECT id, status, severity, alertname, labels, annotations, starts_at, "
        "ends_at, fingerprint, generator_url, source, notified_at, created_at, updated_at "
        "FROM alerts WHERE fingerprint = 'sched-loss-1'",
    )
    assert alert_row is not None
    assert alert_row.status == "resolved"
    assert alert_row.ends_at is not None
