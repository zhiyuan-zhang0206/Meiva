from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from base.telemetry.tests.capture.event_capture import capturing_events
from gateway.tests.agents.gateway_auth import without_credentials
from gateway.tests.auth.gateway_config import gateway_test_client
from gateway.tests.routers.alerts_api.alerts_support import (
    alert,
    alerts_client,
    post_webhook,
    webhook,
)
from gateway.tests.routers.alerts_api.alerts_support import (
    client as client,
)


def _watchdog(**fields: Any) -> dict[str, Any]:
    instance = alert(alertname="Alert pipeline watchdog (synthetic heartbeat)", **fields)
    instance["labels"]["metric"] = "alert_pipeline_watchdog"
    return instance


def _heartbeats(client: TestClient, payload: dict[str, Any]) -> list[float]:
    with capturing_events() as events:
        assert post_webhook(client, payload).status_code == 204
    stamps = [
        event.attributes["last_ingest_timestamp_seconds"]
        for event in events
        if event.event_name == "alerts"
    ]
    assert all(isinstance(stamp, float) for stamp in stamps)
    return [stamp for stamp in stamps if isinstance(stamp, float)]


def test_a_firing_watchdog_refreshes_the_heartbeat(client: TestClient) -> None:
    stamps = _heartbeats(client, webhook(alerts=[_watchdog()]))

    assert len(stamps) == 1
    assert stamps[0] > 0


def test_an_ordinary_alert_is_accepted_and_moves_no_heartbeat(client: TestClient) -> None:
    assert _heartbeats(client, webhook(alerts=[alert()])) == []


def test_a_resolved_watchdog_is_not_a_heartbeat(client: TestClient) -> None:
    payload = webhook(status="resolved", alerts=[_watchdog(status="resolved")])

    assert _heartbeats(client, payload) == []


def test_the_group_status_decides_for_an_alert_that_carries_none(client: TestClient) -> None:
    instance = _watchdog()
    del instance["status"]

    assert len(_heartbeats(client, webhook(status="firing", alerts=[instance]))) == 1
    assert _heartbeats(client, webhook(status="resolved", alerts=[instance])) == []


def test_one_watchdog_among_many_alerts_is_one_heartbeat(client: TestClient) -> None:
    payload = webhook(alerts=[alert(fingerprint="a"), _watchdog(), alert(fingerprint="b")])

    assert len(_heartbeats(client, payload)) == 1


def test_the_envelope_grafana_sends_is_tolerated(client: TestClient) -> None:
    payload = {
        "receiver": "meiva-alerts-webhook",
        "status": "firing",
        "alerts": [_watchdog()],
        "groupLabels": {"alertname": "x"},
        "commonLabels": {},
        "externalURL": "http://grafana",
        "version": "1",
        "groupKey": "k",
        "truncatedAlerts": 0,
    }

    assert len(_heartbeats(client, payload)) == 1


def test_the_webhook_requires_the_token(client: TestClient) -> None:
    payload = webhook(alerts=[_watchdog()])
    without_credentials(client)
    with capturing_events() as events:
        assert client.post("/api/alerts", json=payload).status_code == 401
        legacy = {"X-Ops-Alerts-Token": "test-token"}
        assert client.post("/api/alerts", json=payload, headers=legacy).status_code == 401
        wrong = {"X-Alerts-Token": "wrong"}
        assert client.post("/api/alerts", json=payload, headers=wrong).status_code == 401
    assert [event for event in events if event.event_name == "alerts"] == []
    right = {"X-Alerts-Token": "test-token"}
    assert client.post("/api/alerts", json=payload, headers=right).status_code == 204


def test_a_cluster_bearer_is_accepted() -> None:
    credential = "supersecret"
    with alerts_client(cluster_secret=credential) as client:
        resp = client.post(
            "/api/alerts",
            json=webhook(),
            headers={"Authorization": f"Bearer {credential}"},
        )
    assert resp.status_code == 204


def test_the_webhook_token_works_as_a_bearer(client: TestClient) -> None:
    resp = client.post(
        "/api/alerts", json=webhook(), headers={"Authorization": "Bearer test-token"}
    )
    assert resp.status_code == 204


@pytest.mark.parametrize(
    ("host", "trusted"), [("127.0.0.1", True), ("::1", True), ("10.0.0.5", False)]
)
def test_loopback_is_trusted_only_when_no_token_is_configured(host: str, trusted: bool) -> None:
    with gateway_test_client(fields={"webhook_token": None}, client=(host, 5000)) as client:
        resp = without_credentials(client).post("/api/alerts", json=webhook())
    assert resp.status_code == (204 if trusted else 401)


def test_loopback_is_not_enough_when_a_token_is_set() -> None:
    with alerts_client(client=("127.0.0.1", 5000)) as client:
        resp = without_credentials(client).post("/api/alerts", json=webhook())
    assert resp.status_code == 401
