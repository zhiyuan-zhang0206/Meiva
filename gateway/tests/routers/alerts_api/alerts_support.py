from __future__ import annotations

import json
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Any, cast

import psycopg
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from base.config import settings
from base.events.live.bus import EventBus
from base.telemetry.alerts.native import load_native_group, stamp_native_sent
from gateway.alerts import router as alerts_router
from gateway.tests.auth.gateway_config import GatewayTestClient, gateway_test_client
from ops.alerts import ALERTS_CHANNEL


def native_sender(callback: Callable[[str], bool]) -> Callable[[int], bool]:
    # meiva:note
    # The policy fixture reports real-send success; acceptance-only tests live with Outbox.

    def send(group_id: int) -> bool:
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn:
            source = load_native_group(conn, group_id)
            succeeded = callback(source["text"])
            if succeeded:
                stamp_native_sent(conn, group_id)
            return succeeded

    return send


def alert(
    *,
    status: str = "firing",
    alertname: str = "test-rule",
    severity: str | None = "error",
    starts_at: str = "2026-08-04T10:00:00Z",
    ends_at: str = "",
    summary: str = "test summary",
    fingerprint: str = "abc123",
    notify_im: str | None = None,
) -> dict[str, Any]:
    labels = {"alertname": alertname, "team": "meiva-ops"}
    if severity:
        labels["severity"] = severity
    if notify_im is not None:
        labels["notify_im"] = notify_im
    return {
        "status": status,
        "labels": labels,
        "annotations": {"summary": summary},
        "startsAt": starts_at,
        "endsAt": ends_at,
        "fingerprint": fingerprint,
        "generatorURL": "http://localhost:3002/alerting/xyz/edit",
    }


def webhook(
    status: str = "firing",
    alerts: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": status,
        "alerts": alerts or [alert(status=status, **extra)],
    }
    return payload


# meiva:note
# Test token fixture value.
def ingest(
    client: TestClient,
    payload: dict[str, Any],
    token: str = "test-token",  # noqa: S107
) -> Any:
    return client.post(
        "/api/alerts",
        json=payload,
        headers={"X-Alerts-Token": token},
    )


# meiva:note
# The gateway app with the webhook token set (loopback trust off) and IM fan-out enabled.
#
# `cluster_secret` pins the app's cluster secret; `client_options` reach the test client (the
# address a request appears to come from, for example).
@contextmanager
def alerts_client(
    *, cluster_secret: str | None = None, **client_options: Any
) -> Generator[GatewayTestClient]:
    fields = {"webhook_token": SecretStr("test-token"), "im_notify_enabled": True}
    with gateway_test_client(fields=fields, cluster_secret=cluster_secret, **client_options) as c:
        yield c


# meiva:note
# Every alert row published on the alerts channel while the block runs, decoded.
#
# A real subscriber on the test's Redis: the ingest publishes through its own bus, the frame
# travels the channel, and the list fills when the block ends.
@contextmanager
def alert_frames(bus: EventBus) -> Generator[list[dict[str, Any]]]:
    frames: list[dict[str, Any]] = []
    client = bus.sync_redis(decode_responses=True)
    subscription = cast("Any", client).pubsub()
    try:
        subscription.subscribe(ALERTS_CHANNEL)
        confirmation = subscription.get_message(timeout=2.0)
        assert confirmation is not None and confirmation["type"] == "subscribe"
        yield frames
        while (message := subscription.get_message(timeout=0.5)) is not None:
            if message["type"] == "message":
                frames.append(json.loads(str(message["data"])))
    finally:
        subscription.close()
        client.close()


@pytest.fixture
def client() -> Generator[GatewayTestClient]:
    """TestClient with the gateway app (lifespan: db pool + scheduler)."""
    with alerts_client() as c:
        yield c


def install_im(monkeypatch: pytest.MonkeyPatch, callback: Callable[[str], bool]) -> None:
    # meiva:note
    # The IM bridge is reached by `base.telemetry.alerts.native.notify_alert_group`, which reads
    # the bridge address and the enable switch from the ambient process settings, so the send is
    # the one thing a test of this router cannot redirect by value; it is replaced here, once.
    monkeypatch.setattr(alerts_router, "notify_alert_group", native_sender(callback))


@pytest.fixture(autouse=True)
def alerts_im(monkeypatch: pytest.MonkeyPatch) -> None:
    """IM fan-out accepts every group."""
    install_im(monkeypatch, lambda _text: True)


def seed_alert_rows(db: psycopg.Connection, rows: list[dict[str, Any]]) -> None:
    with db.cursor() as cur:
        for r in rows:
            cur.execute(
                "INSERT INTO alerts"
                " (status, severity, alertname, labels, annotations, starts_at, ends_at,"
                "  fingerprint, source)"
                " VALUES (%(status)s, %(severity)s, %(alertname)s, '{}'::jsonb, '{}'::jsonb,"
                "         %(starts_at)s, %(ends_at)s, %(fingerprint)s, %(source)s)",
                {
                    **r,
                    "source": r.get("source", "grafana"),
                    "labels": None,
                    "annotations": None,
                },
            )
    db.commit()


def capture_im(monkeypatch: pytest.MonkeyPatch, *, ok: bool = True) -> list[str]:
    sent: list[str] = []

    def _capture(text: str) -> bool:
        sent.append(text)
        return ok

    install_im(monkeypatch, _capture)
    return sent
